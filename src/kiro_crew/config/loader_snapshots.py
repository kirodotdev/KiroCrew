"""Published snapshots, so event-loop readers do no config I/O, and their resolvers.

Every ``global`` rebound here has its one home in this module, and the facade serves
it from here. An owner of the :mod:`kiro_crew.config.loader` facade; it never imports
the facade.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.agent_spec_format import iter_agent_spec_files, parse_agent_spec_text
from kiro_crew.config import sections as _sections
from kiro_crew.config.loader_document import (
    ConfigReadError,
    ConfigWriteRefused,
    update_config_locked,
)
from kiro_crew.config.loader_locations import config_path, workspace_dir_from_entry
from kiro_crew.config.paths import kiro_agents_dir, mate_agents_dir, project_agents_dir
from kiro_crew.config.sections import DEFAULT_AUTOCOMPACT_PCT, KiroCrewAgentConfig, WorkspaceConfig

if TYPE_CHECKING:
    from kiro_crew.config.loader_config import KiroCrewConfig

logger = logging.getLogger("kiro_crew.config.loader")


_MATERIALIZED_AGENTS: frozenset[str] = frozenset()

# Filename stem -> declared name, for the configs whose file is NOT named after
# the agent (a package installs ``<Package>-<agent>.json`` declaring ``<agent>``).
# Rewritten IN PLACE by each full refresh, never rebound: the integration
# harness clears it between boots like every other home-derived dict, and its
# residue witness compares a dict by identity. See :func:`dispatch_kiro_agent`.
_MATERIALIZED_STEMS: dict[str, str] = {}
# Names only the crewmate folder declares; see ``mate_worker.is_mate_owned_agent``.
_MATERIALIZED_MATE_ONLY: frozenset[str] = frozenset()

_MATERIALIZED_AGENTS_READY = False

# Whether the snapshot now installed came from a scan that read every spec.
# Published with the snapshot, under the same lock, because the healer reads the
# INSTALLED snapshot, which a later partial scan may have replaced since the
# refresh that called it scanned (see :func:`reset_dangling_default_agent`).
_MATERIALIZED_COMPLETE = False

# Every name a landed snapshot or a publish has let a binding dispatch in this
# process: declared names and the file names mapped to one. Updated in place,
# never rebound. A name in it that the current snapshot does not declare was
# REMOVED, which is the evidence :func:`reset_dangling_default_agent` needs;
# keeping it here rather than diffing two consecutive scans means a refresh that
# does not heal cannot use the evidence up before one that does.
#
# An insertion-ordered dict used as a set, bounded by :func:`_remember_seen`:
# every name the directory still declares moves to the end on each scan, so the
# oldest entries are the removals seen longest ago, and those go first when the
# count passes its bound.
_MATERIALIZED_SEEN: dict[str, None] = {}

# The directory is user-writable and its files choose these names, so the
# retained maps are bounded where they are kept: at most this many entries each,
# and no name (a stem, or the name it maps to) longer than this. A name past the
# length bound is not recorded. For the stem map that leaves the binding
# dispatched verbatim; for the removal evidence it means a default bound to that
# name is never reset, which is the healer's answer to anything it cannot be
# sure of.
_MATERIALIZED_SEEN_MAX = 4096

_MATERIALIZED_SEEN_NAME_MAX_CHARS = 256

# What the last bounded write dropped, so a diagnostic is written when that
# changes rather than on every refresh of an unchanged directory. Keyed by
# ``(what, scope)`` -- see :func:`_report_dropped` for why scope matters.
_MATERIALIZED_LAST_DROPPED: dict[tuple[str, str], int] = {}

# Bumped by every publish. A refresh samples it before scanning and, if it moved
# while the scan was in flight, unions instead of replacing — otherwise a scan
# that globbed the directory BEFORE a registration wrote into it would assign its
# stale view over the just-published names and un-dispatch a freshly enabled app.
_MATERIALIZED_AGENTS_GENERATION = 0

# Monotonic refresh sequencing. A refresh takes a ticket when it STARTS and, on
# completion, discards its result if a refresh that started later already applied:
# two scans race by completion order, not by start order, so an older scan
# finishing second would otherwise overwrite a newer one and resurrect an agent
# that was deleted in between.
_MATERIALIZED_REFRESH_ISSUED = 0

_MATERIALIZED_REFRESH_APPLIED = 0

# Guards the three globals above. Held only for the rebind, never for the scan or
# for a lookup: the read path stays lock-free, which is the whole point of the
# snapshot.
_MATERIALIZED_AGENTS_LOCK = threading.Lock()


def _report_dropped(what: str, dropped: int, *, scope: str = "") -> None:
    """One aggregate line when a bounded write's drop count changes.

    Every bounded map reports through here, once per landed scan or publish
    and only when the count differs from the last report for *what*, so an
    unchanged oversized directory is reported once, not on every refresh.

    *scope* additionally keys the dedup state (e.g. the directory a scan read),
    so two independently-scanned directories reporting the SAME *what* never
    clobber each other's "last reported" value -- without it, a 0-dropped mate
    scan interleaved with a 5-dropped user-level scan would reset the user-level
    dedup state to 0 and make an unchanged user-level directory re-report on
    every refresh (``refresh_materialized_agents`` scans both).
    """
    key = (what, scope)
    if _MATERIALIZED_LAST_DROPPED.get(key, 0) == dropped:
        return
    _MATERIALIZED_LAST_DROPPED[key] = dropped
    if dropped:
        logger.warning(
            "agents directory: %d %s not kept (bounds: %d entries, %d characters per name)",
            dropped,
            what,
            _MATERIALIZED_SEEN_MAX,
            _MATERIALIZED_SEEN_NAME_MAX_CHARS,
        )


def _remember_seen(names: Iterable[str]) -> None:
    """Record *names* in ``_MATERIALIZED_SEEN`` as the newest entries, within its bounds.

    Caller holds ``_MATERIALIZED_AGENTS_LOCK``. A name already present moves to
    the end, so what the directory still declares stays newest and the oldest
    entries are stale removal evidence; those are evicted first once the count
    passes ``_MATERIALIZED_SEEN_MAX``. Overlong names and evictions are counted
    and reported in one line (see :func:`_report_dropped`).
    """
    dropped = 0
    for name in names:
        if len(name) > _MATERIALIZED_SEEN_NAME_MAX_CHARS:
            dropped += 1
            continue
        _MATERIALIZED_SEEN.pop(name, None)
        _MATERIALIZED_SEEN[name] = None
    while len(_MATERIALIZED_SEEN) > _MATERIALIZED_SEEN_MAX:
        del _MATERIALIZED_SEEN[next(iter(_MATERIALIZED_SEEN))]
        dropped += 1
    _report_dropped("removal-evidence name(s)", dropped)
    # NOTE: ``_remember_seen`` draws from the UNION of every scanned directory's
    # names (one shared `_MATERIALIZED_SEEN` map), unlike the per-directory stems
    # bound in ``_scan_materialized_index`` below -- so it needs no *scope* here;
    # it is called once per refresh, not once per directory.


# Whether the last :func:`_scan_materialized_index` on THIS thread read every
# candidate spec. Thread-local, not returned: the scan's ``(names, stems)``
# contract has other callers, and each refresh runs its scan and reads this on
# one thread. A scan that skipped an unreadable or unparseable spec leaves that
# agent's name out of the snapshot although its file is still there, so its
# absence is not evidence of a removal (see :func:`refresh_materialized_agents`).
# ``skipped`` beside it lists the stems of the specs that scan skipped (``None``
# when the listing failed), so a refresh keeps only what those files declared.
_SCAN_STATE = threading.local()


def _scan_materialized_agents(agents_dir: Path) -> frozenset[str]:
    """Every agent name declared by the kiro agent configs in *agents_dir*.

    See :func:`_scan_materialized_index`, which this reads the names half of.
    """
    return _scan_materialized_index(agents_dir)[0]


def _scan_materialized_index(agents_dir: Path) -> tuple[frozenset[str], dict[str, str]]:
    """``(declared names, stem -> declared name)`` for the configs in *agents_dir*.

    The names half is described below. The second half maps a filename stem to
    the name its config declares, only where the two differ, the stem is not
    itself a declared name, and exactly one parsed config has that stem. It lets
    a binding that recorded the FILE name (``KiroPkg-captain`` for a config
    declaring ``captain``) still dispatch the agent the file declares.

    Each config contributes its DECLARED ``name`` field; the filename stem is
    used only as a fallback when a config declares no name, since it is then the
    only identifier available. An app's agent is registered under a namespaced
    filename (``<app>--<agent>.json``) while its config keeps the app's bare
    name, so the stem of a named config is deliberately NOT emitted — kiro-cli
    enumerates agents by declared name and would not resolve it (see the inline
    comment below). Unreadable or non-object entries are skipped. Performs the
    glob and the per-file reads, so callers must invoke it OFF the event loop.
    """
    names: set[str] = set()
    stems: dict[str, str] = {}
    ambiguous: set[str] = set()
    # Deferred import: `hooks` reaches back into this module for config paths, so
    # the edge must resolve lazily. A failure here propagates to
    # refresh_materialized_agents, which logs and leaves the snapshot untouched —
    # fail-closed, rather than falling back to an unguarded read.
    from kiro_crew.hooks import safe_read_file

    _SCAN_STATE.complete = True
    # Stems of the specs this scan skipped; ``None`` when the listing itself
    # failed, so any spec may have been skipped.
    skipped: list[str] = []
    _SCAN_STATE.skipped = skipped
    try:
        candidates = iter_agent_spec_files(agents_dir)
    except OSError:
        _SCAN_STATE.complete = False
        _SCAN_STATE.skipped = None
        return frozenset(), {}
    for af in candidates:
        try:
            # Through the sensitive-path gate, not a bare read: this directory is
            # user-writable, so a symlink planted there (`evil.json` ->
            # `~/.aws/credentials`) would otherwise be read verbatim by a boot
            # refresh. safe_read_file re-checks the RESOLVED target and raises
            # PermissionError for a refused path — an OSError subclass, so a
            # refused entry is skipped by the same handler as an unreadable one.
            data = parse_agent_spec_text(safe_read_file(str(af)), af)
        except (ValueError, OSError):
            # Its agent may be one this scan now leaves out; the scan is partial.
            _SCAN_STATE.complete = False
            skipped.append(af.stem)
            continue
        # Skip stray non-object JSON a user may have dropped in the dir. The
        # filename stem is only trusted AFTER the file parses as an agent config:
        # naming an unparseable file dispatchable would hand kiro-cli a name it
        # cannot load, and it would fall back to its own default silently — the
        # same invisible mismatch this whole change removes.
        if not isinstance(data, dict):
            continue
        # Trust the config's DECLARED `name`, not the filename. `kiro-cli agent
        # list` enumerates agents by their declared name — an app agent written to
        # `mochi--mochi.json` with `"name": "mochi"` is listed as `mochi`, and
        # `mochi--mochi` is not listed at all. Treating the stem as dispatchable
        # would hand kiro-cli a name it does not know, which falls back to its own
        # default silently, an invisible mismatch. The
        # stem is used ONLY when the config declares no name, where it is the only
        # identifier available.
        declared = data.get("name")
        if isinstance(declared, str) and declared:
            names.add(declared)
            if declared != af.stem:
                if af.stem in stems and stems[af.stem] != declared:
                    ambiguous.add(af.stem)
                stems[af.stem] = declared
        else:
            names.add(af.stem)
    # A stem that is itself some agent's declared name already dispatches that
    # agent; one two configs share is no evidence of which was meant. The map is
    # kept for the process, so it is bounded here, where it is built: an
    # overlong stem or name is left out (the binding then dispatches verbatim),
    # and past the count bound the rest are dropped. Both are reported once.
    kept: dict[str, str] = {}
    dropped = 0
    for stem, name in stems.items():
        if stem in names or stem in ambiguous:
            continue
        if (
            len(stem) > _MATERIALIZED_SEEN_NAME_MAX_CHARS
            or len(name) > _MATERIALIZED_SEEN_NAME_MAX_CHARS
            or len(kept) >= _MATERIALIZED_SEEN_MAX
        ):
            dropped += 1
            continue
        kept[stem] = name
    # Scoped by the directory scanned: ``refresh_materialized_agents`` now scans
    # TWO directories (user-level, then the mate scope) per refresh, each with
    # its own independent bound/drop count. Without this scope, the two scans'
    # reports would clobber one shared dedup slot and an unchanged user-level
    # directory could re-report on every refresh purely because the OTHER
    # directory's drop count differed last time.
    _report_dropped("file-name mapping(s)", dropped, scope=str(agents_dir))
    return frozenset(names), kept


def refresh_materialized_agents(*, heal_default: bool = False) -> None:
    """Rescan the kiro agents directory into the in-memory snapshot.

    MUST be called off the event loop — it globs a directory and reads every
    config in it, which scales with agent count. Callers on the loop must use
    :func:`schedule_materialized_agents_refresh` instead.

    Placing the cost on the WRITER is the point: the read path
    (:func:`_materialized_kiro_agent`, reached from ``_run_chat`` ->
    :func:`resolve_agent_bindings` on every turn of an app-bound session) then
    does zero filesystem work. Never raises.

    A scan that lands is the one moment the snapshot is known to be whole, so
    a caller acting for the owner passes ``heal_default=True`` to also run
    :func:`reset_dangling_default_agent`. Healing is opt-in because the reset
    writes the owner's ``config.json``: a refresh reached from anyone else's
    request (an app token's turn, a lookup) must not, and a lazy build can run
    inside a caller's own locked config write (the default-agent PUT reaches
    :func:`dispatch_kiro_agent` from its mutate), where the reset's write would
    wait on the lock that caller holds. A refresh that does not heal still
    records what it saw removed, for the next one that does. A scan that could
    not read or parse every spec never heals: the agent of a skipped file is
    missing from that snapshot while its file is still on disk.

    Consequence worth stating plainly: editing an existing config IN PLACE — say
    renaming its ``name`` field by hand — refreshes nothing, so that new name
    stays undispatchable until the next registration or gateway boot. Hand-editing
    is not how an app agent is meant to appear (``_register_agents`` is), and the
    alternative is filesystem work on the loop, so the staleness is accepted
    rather than papered over with a per-file stat.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_REFRESH_ISSUED
    global _MATERIALIZED_REFRESH_APPLIED, _MATERIALIZED_COMPLETE, _MATERIALIZED_MATE_ONLY
    with _MATERIALIZED_AGENTS_LOCK:
        generation_at_start = _MATERIALIZED_AGENTS_GENERATION
        _MATERIALIZED_REFRESH_ISSUED += 1
        my_ticket = _MATERIALIZED_REFRESH_ISSUED
    try:
        agents_dir = kiro_agents_dir()
        _SCAN_STATE.complete = True
        _SCAN_STATE.skipped = []
        snapshot, stems = _scan_materialized_index(agents_dir)
        # Crewmate scope (``rfc-mate-owned-agent-config`` §5.3). A crewmate owns a
        # custom-agent file in ``~/.kiro/mates/agents/`` whose ``kiro_agent``
        # binding names its id; binding resolution reads both folders so that
        # pointer is a DECLARED agent and the dangling self-heal
        # (:func:`reset_dangling_default_agent`) does not reset it, and dispatch
        # (:func:`dispatch_kiro_agent`) finds it. The session agent picker reads
        # :func:`kiro_agents_dir` only, never this snapshot, so a crewmate stays
        # out of it. Unioned, so a user-level agent of the same name still wins
        # the stem map (user-level is authored; a crewmate id collides only by
        # construction). ``_SCAN_STATE.complete`` is left as the user-level scan
        # set it: a crewmate-folder read error must not mark a complete user-level
        # scan incomplete and block the owner's heal. Its own verdict is kept as
        # ``mate_complete`` instead: an incomplete crewmate scan keeps every
        # crewmate name the last snapshot held, so a file it could not read is
        # never removal evidence.
        complete_after_user = bool(getattr(_SCAN_STATE, "complete", True))
        skipped_user: list[str] | None = getattr(_SCAN_STATE, "skipped", None)
        _SCAN_STATE.complete = True
        mate_names, mate_stems = _scan_materialized_index(mate_agents_dir())
        mate_complete = bool(getattr(_SCAN_STATE, "complete", True))
        _SCAN_STATE.complete = complete_after_user
        # The mate conductor and worker live in that folder too
        # (``agent_files.MATE_SERVICE_AGENT_FILENAMES``): declared agents, never a
        # crewmate's own file.
        from kiro_crew.agent_files import MATE_SERVICE_AGENT_NAMES

        mate_only = frozenset(mate_names - snapshot - MATE_SERVICE_AGENT_NAMES)
        user_names = snapshot
        snapshot = frozenset(snapshot | mate_names)
        stems = {**mate_stems, **stems}
        scan_complete = complete_after_user
    except Exception:  # noqa: BLE001 — a refresh failure only costs a fallback
        logger.debug("Failed to refresh materialized agent names", exc_info=True)
        return
    with _MATERIALIZED_AGENTS_LOCK:
        if my_ticket < _MATERIALIZED_REFRESH_APPLIED:
            # A refresh that started AFTER this one already applied, so this view
            # is older than what is installed. Assigning it would undo the newer
            # scan — resurrecting an agent deleted in between, whose config is gone
            # from disk. Drop it; the newer snapshot already reflects reality.
            logger.debug("Discarding out-of-order materialized agent refresh")
            return
        if _MATERIALIZED_AGENTS_GENERATION != generation_at_start:
            # A registration published while this scan was in flight, so the scan
            # may have globbed the directory before that write landed. Replacing
            # would erase the published names and un-dispatch a freshly enabled
            # app; union instead and let the refresh scheduled by that
            # registration apply the authoritative view (including removals).
            snapshot = frozenset(snapshot | _MATERIALIZED_AGENTS)
            # The same for a crewmate file published meanwhile
            # (:func:`publish_mate_agents`), unless this scan saw the user level
            # declare that name.
            mate_only = frozenset(mate_only | (_MATERIALIZED_MATE_ONLY - user_names))
        if not mate_complete:
            kept = _MATERIALIZED_MATE_ONLY - user_names
            snapshot = frozenset(snapshot | kept)
            mate_only = frozenset(mate_only | kept)
        kept_stems: frozenset[str] = frozenset()
        if not scan_complete:
            # The user level keeps what a SKIPPED spec declared last time: that
            # file is still on disk, and dropping its name would fail every
            # session bound to it until the next complete refresh. Only those
            # names stay -- the skipped file's stem, or the name its stem mapped
            # to -- so a file really removed meanwhile (an app disable, a
            # delete) still drops out. A failed listing skipped everything.
            previous_user = _MATERIALIZED_AGENTS - _MATERIALIZED_MATE_ONLY
            if skipped_user is None:
                kept_user = previous_user
                kept_stems = frozenset(_MATERIALIZED_STEMS)
            else:
                kept_stems = frozenset(s for s in skipped_user if s in _MATERIALIZED_STEMS)
                declared = {*skipped_user, *(_MATERIALIZED_STEMS[s] for s in kept_stems)}
                kept_user = previous_user & declared
            snapshot = frozenset(snapshot | kept_user)
            mate_only = frozenset(mate_only - kept_user)
        _MATERIALIZED_AGENTS = snapshot
        _MATERIALIZED_MATE_ONLY = mate_only
        # In place, new entries first: a reader between the two statements sees
        # every current stem (an old value for one just rewritten, never a hole),
        # and the dict's identity holds across refreshes and boots. A skipped
        # spec's mapping is kept, for the reason above.
        _MATERIALIZED_STEMS.update(stems)
        for stale in [s for s in _MATERIALIZED_STEMS if s not in stems and s not in kept_stems]:
            del _MATERIALIZED_STEMS[stale]
        # The scan bounds what it built; the kept mappings come on top, so the
        # merged map is bounded here too, dropping kept entries before fresh ones.
        overflow = max(0, len(_MATERIALIZED_STEMS) - _MATERIALIZED_SEEN_MAX)
        for extra in [s for s in _MATERIALIZED_STEMS if s not in stems][:overflow]:
            del _MATERIALIZED_STEMS[extra]
        _report_dropped("kept file-name mapping(s)", overflow)
        # One call per landed snapshot, so its eviction and its report happen
        # once for the whole of what it declares.
        _remember_seen([*snapshot, *stems])
        _MATERIALIZED_COMPLETE = scan_complete
        _MATERIALIZED_AGENTS_READY = True
        _MATERIALIZED_REFRESH_APPLIED = my_ticket
    # An app install/upgrade that rewrote agent JSON just landed in the snapshot;
    # drop the context builder's per-agent includeCrewContext cache so the next
    # build re-reads the flag rather than serving a value cached before the write
    # (otherwise a flipped flag heals only on gateway restart).
    try:
        from kiro_crew.context import invalidate_include_crew_context_cache

        invalidate_include_crew_context_cache()
    except Exception:  # noqa: BLE001 — best-effort; a stale flag is not fatal
        logger.debug("Failed to invalidate includeCrewContext cache", exc_info=True)
    # Only a scan that read every spec may heal: one that skipped a file it could
    # not read or parse is missing that file's agent while the file is still
    # there, and the reset it would make is durable.
    if heal_default and scan_complete:
        reset_dangling_default_agent()


def _removal_evidence() -> tuple[frozenset[str] | None, frozenset[str], bool]:
    """``(installed names, removed names, scan complete)``, read in one lock hold.

    ``None`` names while no snapshot has landed. Read together so the removals
    and the completeness flag describe the same installed snapshot.
    """
    with _MATERIALIZED_AGENTS_LOCK:
        names = _MATERIALIZED_AGENTS if _MATERIALIZED_AGENTS_READY else None
        removed = frozenset(
            _MATERIALIZED_SEEN.keys() - _MATERIALIZED_AGENTS - _MATERIALIZED_STEMS.keys()
        )
        return names, removed, _MATERIALIZED_COMPLETE


def materialized_snapshot_complete() -> bool:
    """Whether the installed snapshot came from a scan that read every spec.

    A pure in-memory read, safe on the event loop. ``False`` before the first
    refresh lands, and after one that skipped a spec it could not read: a name
    missing then may still have its file on disk, so a caller about to fail a
    turn on that miss rescans once off the loop first (``_turn_admit``).
    """
    return _MATERIALIZED_COMPLETE


def reset_dangling_default_agent() -> bool:
    """Set ``default_agent`` back to ``default`` when its template file is gone.

    An agent the owner made the default can lose its spec after the fact: a
    package uninstall removes its file, a disabled app deregisters its
    agents, a user deletes the file by hand. The alias row stays in
    ``config.json`` and ``default_agent`` still names it, so every new chat
    dispatches a name kiro-cli does not list and fails "Agent mode ... is not
    available". The owner's call for that state is the shipped ``default`` crew,
    which binds the managed template, so this points ``default_agent`` there.
    The alias row is left alone: it is the owner's record, and the template may
    come back.

    "Gone" is a REMOVED name: one an earlier landed snapshot (or a publish) in
    this process let a binding dispatch, and the current snapshot does not.
    Absence alone is not evidence. A
    binding can name an agent this directory never declared and still
    dispatch: a project checkout's own spec (kiro-cli resolves ``--agent``
    against the session's directory first) or an agent the edition contributes.
    For the same reason a removed name the edition still supplies is kept.

    Decides against the materialized snapshot only, so it is a pure in-memory
    check between the config load and the write, and it never acts on
    uncertainty -- a cold or empty snapshot, nothing removed, an unreadable or
    degraded config, a missing ``default`` alias, or a default already
    ``default`` all leave the file untouched. The managed ``kirocrew`` template is exempt: a missing one is
    regenerated before spawn (``agent.ensure_agent_materialized``), not a loss.
    The write is the same locked read-modify-write the default-agent PUT uses,
    and both conditions are re-derived inside the lock, so a PUT that repointed
    the default in the window wins. Runs OFF the event loop (it loads the config
    and takes the config lock); :func:`refresh_materialized_agents` is its
    caller. Never raises. Returns ``True`` when the default was reset.
    """
    # KiroCrewConfig sits above this module (it dispatches through this snapshot),
    # so it is imported where it is used.
    from kiro_crew.config.loader_config import KiroCrewConfig

    names, removed, complete = _removal_evidence()
    if not names or not removed or not complete:
        return False
    try:
        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 — an unreadable config is exactly when not to act
        logger.debug("default agent check: config unreadable", exc_info=True)
        return False
    if cfg.degraded_sections:
        return False
    current = cfg.default_agent
    row = cfg.agents.get(current)
    if current == "default" or row is None or "default" not in cfg.agents:
        return False
    template = _dangling_template(row.kiro_agent, names)
    if template is None or template not in removed or template in _edition_agent_names():
        return False
    workspace_dir = _alias_workspace_dir(cfg.workspaces, row.workspace, cfg.default_workspace)
    if _workspace_declares(template, workspace_dir):
        return False
    reset = False

    def _reset(data: dict) -> dict | None:
        nonlocal reset
        agents = data.get("agents")
        if data.get("default_agent") != current or not isinstance(agents, dict):
            return None
        row = agents.get(current)
        if "default" not in agents or not isinstance(row, dict):
            return None
        # Re-derived from the snapshot installed NOW: another refresh may have
        # replaced the one read above, and if its scan was partial its absences
        # are not evidence.
        names_now, removed_now, complete_now = _removal_evidence()
        if names_now is None or not complete_now or template not in removed_now:
            return None
        if _dangling_template(row.get("kiro_agent"), names_now) != template:
            return None
        # The workspace checked above must still be the one this row places in;
        # a row repointed at another workspace in the window was not checked.
        if _locked_workspace_dir(data, row) != workspace_dir:
            return None
        data["default_agent"] = "default"
        reset = True
        return data

    try:
        update_config_locked(config_path(), mutate=_reset, stamp_meta=False)
    except (ConfigReadError, ConfigWriteRefused, OSError):
        logger.debug("default agent check: config write declined", exc_info=True)
        return False
    if not reset:
        return False
    logger.warning(
        "default agent %r runs template %r, whose file is gone from %s; "
        "default_agent reset to 'default'",
        current,
        template,
        kiro_agents_dir(),
    )
    _log_default_agent_reset(current, template)
    return True


def _alias_workspace_dir(
    workspaces: Mapping[str, WorkspaceConfig], name: str, default_name: str
) -> Path:
    """The directory an alias's ``workspace`` places it in, as dispatch does.

    The named entry, else the ``default_workspace`` entry, through
    :func:`workspace_dir_from_entry`, the one placement rule.
    """
    return workspace_dir_from_entry(workspaces.get(name) or workspaces.get(default_name))


def _locked_workspace_dir(data: dict, row: dict) -> Path | None:
    """:func:`_alias_workspace_dir` for a raw ``config.json`` document and row.

    ``None`` when the document's ``workspaces`` section is not the plain
    name -> ``{"dir": ...}`` shape, so a caller comparing it declines.
    """
    # KiroCrewConfig sits above this module (it dispatches through this snapshot),
    # so it is imported where it is used.
    from kiro_crew.config.loader_config import KiroCrewConfig

    raw = data.get("workspaces", {})
    if not isinstance(raw, dict):
        return None
    entries: dict[str, WorkspaceConfig] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("dir", ""), str):
            return None
        entries[name] = WorkspaceConfig(dir=entry.get("dir", ""))
    name = row.get("workspace") or KiroCrewAgentConfig.__dataclass_fields__["workspace"].default
    default_name = (
        data.get("default_workspace")
        or KiroCrewConfig.__dataclass_fields__["default_workspace"].default
    )
    if not isinstance(name, str) or not isinstance(default_name, str):
        return None
    return _alias_workspace_dir(entries, name, default_name)


def _workspace_declares(template: str, workspace_dir: Path) -> bool:
    """Whether the checkout at *workspace_dir* still declares *template*.

    The removal evidence comes from the user-level agents directory only, but
    kiro-cli resolves ``--agent`` against the session's project directory first,
    so a global spec uninstalled while the alias's workspace still declares a
    same-named spec leaves the default dispatching there. Read with the same
    scan the user-level snapshot uses, over ``<workspace>/.kiro/agents`` (the
    only project location the backend activates, both spec forms), so its
    completeness is known. Answers ``True`` -- do not reset -- whenever it cannot
    be sure: a sensitive workspace directory (never read), a listing that fails,
    or a scan that skipped a spec it could not read or parse. A workspace with
    no ``.kiro/agents`` directory, or none at all, declares nothing.
    """
    from kiro_crew.security import is_sensitive_path  # circular import

    try:
        if is_sensitive_path(str(workspace_dir)):
            return True
        agents_dir = project_agents_dir(workspace_dir)
        if not agents_dir.exists():
            return False
        previous = getattr(_SCAN_STATE, "complete", True)
        try:
            names, _stems = _scan_materialized_index(agents_dir)
            complete = bool(getattr(_SCAN_STATE, "complete", True))
        finally:
            _SCAN_STATE.complete = previous
    except Exception:  # noqa: BLE001 — an unreadable checkout is not evidence of removal
        logger.debug("default agent check: workspace agents unreadable", exc_info=True)
        return True
    return not complete or template in names


_EDITION_LOOKUP = threading.local()


def _edition_agent_names() -> frozenset[str]:
    """Names of the agents the edition contributes beside the on-disk specs.

    Taken from ``agent_discovery._with_edition_agents``, the one reader of the
    edition's agent catalog, so the healer and the agent listing agree on which
    rows exist: its failure handling (none on an adapter error) and its row
    validation apply here unchanged. The public edition contributes none.

    Re-entry on the same thread answers none, the same answer as an adapter
    error. With no installed context the lookup builds one, which loads config,
    which can reach this lookup again. Unguarded, that cycle runs to the
    recursion limit, and a GC finalizer at that depth fails whatever is running.
    """
    if getattr(_EDITION_LOOKUP, "active", False):
        return frozenset()
    # Deferred: agent_discovery imports this package's paths at load time.
    from kiro_crew.agent_discovery import _with_edition_agents

    _EDITION_LOOKUP.active = True
    try:
        return frozenset(agent.name for agent in _with_edition_agents([]))
    finally:
        _EDITION_LOOKUP.active = False


def _dangling_template(kiro_agent: object, names: frozenset[str]) -> str | None:
    """The template a ``kiro_agent`` binding runs, when *names* does not declare it.

    ``None`` for a healthy binding, and for the managed ``kirocrew`` template
    (bound explicitly or by an empty binding), which self-heals elsewhere. The
    binding goes through :func:`dispatch_kiro_agent` so a row that recorded a
    file name counts as the agent that file declares.
    """
    bound = kiro_agent if isinstance(kiro_agent, str) else ""
    template = dispatch_kiro_agent(bound) or "kirocrew"
    if template == "kirocrew" or template in names:
        return None
    return template


def _log_default_agent_reset(alias: str, template: str) -> None:
    """Best-effort SEL record of a default-agent reset, like the clamp event."""
    try:
        from kiro_crew.sel import SecurityEvent, sel

        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex[:16],
                timestamp=datetime.now(timezone.utc).isoformat(),
                event_type="default_agent_reset",
                caller_identity="config_loader",
                agent="",
                source="background",
                operation="config.default_agent",
                outcome="reset",
                resources=f"agent:{alias}",
                metadata={"template": template, "reset_to": "default"},
            )
        )
    except Exception:
        logger.debug("SEL default-agent-reset event failed", exc_info=True)


def publish_materialized_agents(names: Iterable[str]) -> None:
    """Add *names* to the snapshot immediately, with no filesystem access.

    A pure set union — safe to call from anywhere, including the event loop.
    ``apps.bridges._register_agents`` uses it to publish the agents it just wrote
    BEFORE scheduling the full rescan, because the rescan can be delayed
    arbitrarily when the default executor is saturated, and the window is not
    merely cosmetic: a slot created in it is normalized to the agent that answers
    (the default) and that substitution is STORED, so the slot would stay bound to
    the default agent rather than recovering on the next turn.

    The snapshot is marked ready, which is safe in both contexts: on the loop the
    scheduled rescan fills in everything else moments later, and in a synchronous
    context the scheduler rescans inline, so the union is immediately superseded
    by a complete snapshot.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_AGENTS_GENERATION
    fresh = {n for n in names if isinstance(n, str) and n}
    if not fresh:
        return
    with _MATERIALIZED_AGENTS_LOCK:
        _MATERIALIZED_AGENTS = frozenset(_MATERIALIZED_AGENTS | fresh)
        _remember_seen(fresh)
        _MATERIALIZED_AGENTS_READY = True
        # Signals any in-flight refresh that its view predates this write, so it
        # unions rather than replacing (see refresh_materialized_agents).
        _MATERIALIZED_AGENTS_GENERATION += 1


def publish_mate_agents(names: Iterable[str]) -> None:
    """Record *names* as crewmate-owned files the moment they are written.

    The crewmate writers (``mate_config.write_mate_spec``) call this right
    after the file lands, so the next chat of that crewmate runs on the
    conductor (:func:`kiro_crew.mate_worker.member_chat_agent`) instead of
    waiting for the next full scan. A name the user level already declares is
    left out of the crewmate-only set, the rule the full scan applies. A pure set
    union, safe on the event loop.
    """
    global _MATERIALIZED_AGENTS, _MATERIALIZED_AGENTS_READY, _MATERIALIZED_AGENTS_GENERATION
    global _MATERIALIZED_MATE_ONLY
    fresh = {n for n in names if isinstance(n, str) and n}
    if not fresh:
        return
    with _MATERIALIZED_AGENTS_LOCK:
        user_level = (
            _MATERIALIZED_AGENTS - _MATERIALIZED_MATE_ONLY
            if _MATERIALIZED_AGENTS_READY
            else frozenset()
        )
        _MATERIALIZED_MATE_ONLY = frozenset(_MATERIALIZED_MATE_ONLY | (fresh - user_level))
        _MATERIALIZED_AGENTS = frozenset(_MATERIALIZED_AGENTS | fresh)
        _remember_seen(fresh)
        _MATERIALIZED_AGENTS_READY = True
        _MATERIALIZED_AGENTS_GENERATION += 1


def schedule_materialized_agents_refresh() -> None:
    """Refresh the snapshot from ANY context without blocking an event loop.

    ``apps.bridges._register_agents`` is the writer that must trigger this. The
    dashboard enable/update handlers dispatch ``register_app`` to an executor
    thread, so from those paths this runs in a synchronous context (no running
    loop) and refreshes inline — the scan is already off the loop, serialized
    inside the awaited registration, so no stale-snapshot window exists there.
    The same inline branch covers the CLI, tests, and the boot warm already on
    an executor. For a caller that does hold a live loop, scanning inline would
    be the same directory-walk-per-agent-file stall the neighbouring prune
    comment warns about, so the scan is handed to the default executor and this
    returns immediately; that offloaded refresh lands a few milliseconds later,
    and a turn dispatched in that window sees the previous snapshot for one
    turn, then self-heals — strictly better than staying stale until the next
    gateway boot. Never raises; the scan itself swallows its errors.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        refresh_materialized_agents()
        return
    try:
        # Fire-and-forget on purpose: nothing awaits this, and
        # refresh_materialized_agents never raises, so the discarded future
        # cannot surface an unretrieved exception.
        loop.run_in_executor(None, refresh_materialized_agents)
    except Exception:  # noqa: BLE001 — a scheduling failure only costs a fallback
        logger.debug("Failed to schedule materialized agent refresh", exc_info=True)


def dispatch_kiro_agent(kiro_agent: str) -> str:
    """The name kiro-cli knows for a crewmate's bound ``kiro_agent``.

    A crewmate created by hand, or by an older guide, can record the agent's
    FILE name (``KiroPkg-captain``, from ``~/.kiro/agents/KiroPkg-captain.json``)
    while the file declares ``"name": "captain"``. kiro-cli lists agents by
    declared name only, so dispatching the file name fails closed with "Agent
    mode ... is not available". This maps such a stem to the name its file
    declares, so the existing crewmate works without editing its row.

    Changes nothing else: a name that is itself declared, a stem two files share,
    a name the edition contributes, and any unknown name are returned verbatim. A pure in-memory lookup (see
    :func:`_materialized_kiro_agent` for why), built by the same full refresh.
    """
    if not kiro_agent or kiro_agent in _MATERIALIZED_AGENTS:
        return kiro_agent
    if not _MATERIALIZED_AGENTS_READY:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            refresh_materialized_agents(heal_default=False)
        else:
            return kiro_agent
    mapped = _MATERIALIZED_STEMS.get(kiro_agent)
    if mapped is None:
        return kiro_agent
    # A name the edition contributes is that agent, whatever a local file of the
    # same stem declares: the directory is user-writable, so a file must not be
    # able to redirect a binding to an edition agent onto some other agent.
    # Asked only when a mapping exists, so the common path stays in memory.
    if kiro_agent in _edition_agent_names():
        return kiro_agent
    return mapped


def member_template_id(agent_cfg: KiroCrewAgentConfig) -> str:
    """The template a crewmate row runs: its ``kiro_agent`` through
    :func:`dispatch_kiro_agent`, ``kirocrew`` when the row binds none.

    The one rule for turning a row into an ``ExecutionContext.template_id``.
    Every dispatch path reads that field verbatim -- the subagent run hands it
    to kiro-cli, ``resolve_session_agent_bindings`` republishes it as the
    session's agent -- so a file-name binding is mapped where the row is read,
    before the record exists, by each builder (:func:`resolve_member_execution`
    and the subagent admission gate's resume and inherited-member arms).
    """
    from kiro_crew.mate_worker import member_chat_agent

    return member_chat_agent(agent_cfg.kiro_agent) or "kirocrew"


#: Agents kiro-cli ships inside its own binary. No spec file declares them in any
#: agents directory, so no scan below can find them, yet kiro-cli launches and
#: switches to them like any other: ``/agent kiro_planner`` (or ``/plan``) moves a
#: session onto one. The set is what ``kiro-cli acp`` advertises in
#: ``availableModes`` and starts as ``currentModeId`` under ``--agent``, measured
#: against kiro-cli 2.21.1. It is NOT ``kiro-cli agent list``'s ``(Built-in)``
#: rows: ``kiro_help`` is listed there but is not an ACP agent (``--agent
#: kiro_help`` silently starts ``kiro_default``), and ``kiro_guide`` is the ACP
#: one. A spec file of the same name, when one exists, is what kiro-cli loads
#: instead; the answer here is the same name.
KIRO_CLI_BUILTIN_AGENTS = frozenset({"kiro_default", "kiro_guide", "kiro_planner"})


def _materialized_kiro_agent(agent_name: str | None, project_dir: str | None = None) -> str:
    """Return *agent_name* when a materialized kiro agent config declares it.

    An APP's agents are copied into ``~/.kiro/agents/`` by
    ``apps.bridges._register_agents`` under a namespaced FILENAME
    (``<app>--<agent>.json``) while the config inside keeps the app's own bare
    ``name``. Nothing adds them to ``config.agents`` — that mapping is authored
    by setup / the user — so an app agent is resolvable by kiro-cli but is NOT a
    Kiro Crew alias. Without this lookup :func:`resolve_agent_bindings` would fall
    all the way back to ``default_agent`` and silently dispatch the DEFAULT kiro
    agent for a session the user explicitly bound to an app's agent: the slot
    still shows the requested name (it is stored verbatim, unvalidated), so the
    UI claims "mochi" while the default agent answers, without the app's MCP
    tools.

    A pure in-memory set membership test — NO filesystem I/O, not even a stat.
    This is reached from ``_run_chat`` -> :func:`resolve_agent_bindings` on EVERY
    turn of an app-bound session (an app agent is never an alias, so it always
    takes this path), and a scan there would stall chat, WebSocket and heartbeat
    processing. The snapshot is refreshed only off-loop, by the gateway at boot
    and by ``_register_agents`` / ``_deregister_agents`` around their writes (see
    :func:`refresh_materialized_agents`).

    CONTRACT, stated deliberately because it is wider than the bug it fixes: this
    honors ANY parseable agent config in the directory, not only app-registered
    ones, and grafts the DEFAULT agent's workspace and memory bindings onto it. An
    agent created by kiro-cli's own flow, or dropped in by hand, therefore becomes
    dispatchable with default bindings — it is not restricted to
    ``bridges._register_agents`` output. That is intentional: the directory is the
    kiro-cli agent registry, every entry in it is a real agent kiro-cli can load,
    and narrowing to app-registered names would mean tracking provenance the
    directory does not record. It is safe inside the single-user trust boundary,
    and reads go through the sensitive-path gate (see
    :func:`_scan_materialized_agents`), but it IS a wider surface than "app agents
    dispatch" and should be read as such.

    When no snapshot exists yet, one is built lazily ONLY in a synchronous
    context (the CLI, tests) — never while an event loop is running, where an
    unwarmed lookup falls back to the default rather than block. Returns ``""``
    for a blank name or when nothing declares it, so a genuinely unknown agent
    still falls back to the default.

    *project_dir* adds the session's own ``<project>/.kiro/agents`` scope, which
    kiro-cli searches BEFORE the user-level directory (it resolves ``--agent``
    against its cwd, and Kiro Crew spawns it with the project dir as cwd). It
    deliberately does NOT use the snapshot: that is one process-wide set, while the
    project scope differs per session, so sharing it would leak one checkout's
    agents into another's.

    The project lookup reads the filesystem, so like the user-level scan it is
    NEVER performed on the event loop — see :func:`_project_declares_agent`. Callers
    that need a project agent resolved must therefore invoke this off the loop;
    ``chat_runner`` and the side-turn handler do so through the discovery pool. An
    on-loop call degrades to the default agent for that turn rather than stalling
    the gateway, which is the same trade the user-level scope already makes.
    """
    if not agent_name:
        return ""
    if agent_name in KIRO_CLI_BUILTIN_AGENTS:
        # kiro-cli's own agents have no file to find (see the constant). Without
        # this, a switch to one is recorded and then refused on the next turn.
        return agent_name
    if _MATERIALIZED_AGENTS_READY and agent_name in _MATERIALIZED_AGENTS:
        return agent_name
    if not _MATERIALIZED_AGENTS_READY:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No loop on this thread: scanning here blocks nothing. The lazy
            # build only answers this lookup (see refresh_materialized_agents).
            refresh_materialized_agents(heal_default=False)
            if agent_name in _MATERIALIZED_AGENTS:
                return agent_name
        else:
            # On the event loop with a cold snapshot: never scan. The boot warm
            # normally precedes any turn; falling back for one turn is strictly
            # preferable to stalling the gateway.
            logger.debug("Materialized agent snapshot cold on the event loop; falling back")
    if project_dir and _project_declares_agent(agent_name, project_dir):
        return agent_name
    return ""


# Snapshot of the Kiro Crew agent ALIAS table as ONE immutable
# ``(aliases, default_template, ready)`` triple — the keys of ``config.agents``,
# the TEMPLATE an unhonored request falls back to (:func:`default_template`, the
# same answer :func:`resolve_agent_bindings` gives: a session whose name is no
# alias and no materialized template runs the default custom agent, never a
# crewmate), and whether a load has published yet. Refreshed by every successful
# :meth:`KiroCrewConfig.load`, exactly like ``_MATERIALIZED_AGENTS`` is refreshed
# by every scan, and for the same reason: the read path
# (:func:`resolve_effective_agent`, reached from ``_ChatSlot.to_dict`` for every
# slot of every slots frame) must do ZERO filesystem work, and ``config.agents``
# is otherwise only reachable by re-reading and re-validating ``config.json``.
#
# One tuple rather than three globals, and no lock, deliberately: publishing is a
# single rebind of a single name, so a reader either sees the whole previous
# triple or the whole new one. Three separate globals would need a lock to stop a
# reader pairing the new alias set with the old fallback name, and that lock would
# then be acquired once per slot per frame on the event loop. Immutability is what
# removes the need for it — never mutate the tuple or the frozenset in place.
#
# ``ready=False`` reads as "no opinion", not "nothing configured": the resolver
# reports no divergence rather than guessing, because a wrong "your agent was
# substituted" marker is worse than none at all.
_CONFIG_AGENT_ALIAS_SNAPSHOT: tuple[frozenset[str], str, bool] = (frozenset(), "", False)


def publish_agent_alias_snapshot(config: "KiroCrewConfig") -> None:
    """Publish *config*'s alias table for the filesystem-free display resolver.

    Pure in-memory rebind — safe from anywhere, including the event loop. Called
    from :meth:`KiroCrewConfig.load` so every successful load refreshes it,
    including the degraded-defaults path (which must OVERWRITE a richer previous
    snapshot rather than leave a resolver claiming aliases that do not load).

    The fallback published is :func:`default_template` read at publish time, so
    the display resolver names the template the runtime resolver would bind,
    including the managed fallback for a configured template the landed
    snapshot shows removed.
    """
    global _CONFIG_AGENT_ALIAS_SNAPSHOT
    aliases = frozenset(str(n) for n in config.agents if isinstance(n, str) and n)
    _CONFIG_AGENT_ALIAS_SNAPSHOT = (aliases, default_template(config), True)


def agent_alias_snapshot() -> tuple[frozenset[str], str, bool]:
    """The published alias table as ``(aliases, default_template, ready)``."""
    return _CONFIG_AGENT_ALIAS_SNAPSHOT


# Snapshot of the auto-compaction threshold, refreshed by every successful
# :meth:`KiroCrewConfig.load`, for the same reason as the two snapshots above:
# the read path (``SessionManager._compaction_gate_decision``) runs on the event
# loop after every turn, so it must never stat/read/validate config.json itself.
#
# What this specifically buys, beyond avoiding that I/O: a config write from ANY
# writer reaches a running gateway. The dashboard PATCH handler and the CLI both
# end at ``update_config_locked``, and only the handler could notify the manager
# it had changed something -- so a ``kirocrew config set`` landed on disk while
# the live threshold kept its startup value until a restart. Publishing on load
# closes that without either writer having to know which live object holds it.
#
# Ordered by a monotonically increasing TICKET drawn before each load's read.
# Loads run concurrently (prompt assembly, background threads), so without
# ordering an older load finishing last would republish the value it read before
# a newer write -- leaving live sessions compacting at an obsolete threshold
# until something loaded again. The two snapshots above carry the same race; the
# consequence there is a display marker, which is why only this one is ordered.
#
# A ticket rather than the files' newest ``st_mtime_ns``, because this ordering
# must be monotonic and an mtime is not. Deleting the newer of the two config
# files LOWERS that maximum, and so does restoring a backup with ``cp -p`` or any
# other writer that preserves timestamps; each one makes the current state of the
# filesystem look like an older read, so the publish that should win is dropped
# and the live gate keeps a threshold the current files do not carry. A ticket is
# independent of the filesystem, so a deletion and a timestamp-preserving restore
# both order as what they are: the newest read.
_CONFIG_AUTOCOMPACT_PCT: float = DEFAULT_AUTOCOMPACT_PCT

_CONFIG_AUTOCOMPACT_TICKET: int = 0

#: Highest ticket handed out by :func:`next_config_load_ticket`. Distinct from the
#: PUBLISHED ticket above: a load draws one and can still lose the comparison,
#: which must not move the published mark.
_CONFIG_AUTOCOMPACT_ISSUED: int = 0

#: Serializes the ticket draw and the compare-and-set in
#: :func:`publish_autocompact_pct`. Held ONLY on those two write paths, each of
#: which runs inside ``load()`` and is therefore already doing file I/O and schema
#: validation -- the lock is free by comparison. The READ path
#: (:func:`published_autocompact_pct`) never takes it, which is what keeps the
#: event loop lock-free; that is the objection the alias snapshot above avoids by
#: publishing one immutable tuple, and it does not apply to a write-side lock.
#:
#: Needed because each path is a read followed by a write: without it two
#: concurrent loads can draw the SAME ticket, or both pass the publish comparison
#: and let whichever assigns LAST win, so an older read replaces a newer one and
#: rolls the published ticket backwards with it.
_CONFIG_AUTOCOMPACT_LOCK = threading.Lock()


def next_config_load_ticket() -> int:
    """Draw the next config-load ordering ticket.

    Call this BEFORE the read whose result will be published, so the ticket
    records when this load began observing the files. Two loads whose reads
    interleave are then ordered by ticket rather than by anything on disk: the
    loser's value is at most microseconds stale and the next load corrects it,
    where an unordered publish can leave an obsolete threshold in force
    indefinitely.

    Never returns 0, so 0 means "nothing published yet".
    """
    global _CONFIG_AUTOCOMPACT_ISSUED
    with _CONFIG_AUTOCOMPACT_LOCK:
        _CONFIG_AUTOCOMPACT_ISSUED += 1
        return _CONFIG_AUTOCOMPACT_ISSUED


def publish_autocompact_pct(config: "KiroCrewConfig", ticket: int | None = None) -> None:
    """Publish *config*'s compaction threshold for the filesystem-free read path.

    Pure in-memory rebind -- safe from anywhere, including the event loop, and a
    reader sees either the whole previous value or the whole new one. Called from
    :meth:`KiroCrewConfig.load` so every successful load refreshes it, including
    the degraded-defaults path, which must OVERWRITE a previous snapshot rather
    than leave a stale threshold in force.

    *ticket* orders this publish against concurrent ones. It must come from
    :func:`next_config_load_ticket`, drawn BEFORE the read that produced *config*;
    a ticket lower than the one already published is dropped. Omitting it draws a
    fresh ticket, which therefore always wins -- correct for a caller that has
    just built the config it is publishing (tests), and wrong for one replaying an
    earlier read, which must pass the ticket it drew.

    No ticket value is special-cased. "Neither config file exists" is the current
    truth rather than an older read of the same file, and it arrives here on the
    degraded-defaults path holding a freshly drawn ticket, so it wins by ordinary
    comparison. Being able to state that without a carve-out is the reason the
    ticket is independent of the files: an ordering read off their mtime drops to
    a lower value when a file is removed, and so cannot express it.
    """
    global _CONFIG_AUTOCOMPACT_PCT, _CONFIG_AUTOCOMPACT_TICKET
    # Drawn OUTSIDE the lock: next_config_load_ticket acquires the same
    # non-reentrant lock, so drawing it inside the block below would deadlock.
    if ticket is None:
        ticket = next_config_load_ticket()
    # Compare and BOTH assignments under one lock: they are a single
    # compare-and-set, and splitting them lets two concurrent loads both pass the
    # comparison and race the writes. See _CONFIG_AUTOCOMPACT_LOCK.
    with _CONFIG_AUTOCOMPACT_LOCK:
        if ticket < _CONFIG_AUTOCOMPACT_TICKET:
            return
        _CONFIG_AUTOCOMPACT_TICKET = ticket
        _CONFIG_AUTOCOMPACT_PCT = config.session.autocompact_pct


def published_autocompact_pct() -> float:
    """The published compaction threshold."""
    return _CONFIG_AUTOCOMPACT_PCT


# Snapshot of the global default timezone, refreshed by every successful
# :meth:`KiroCrewConfig.load`, for the same reason as the three snapshots above:
# its read path runs on the event loop. ``kiro_crew.cron._job_tz`` is reached
# from ``CronService._on_timer``'s due-scan for EVERY cron-expression job that
# does not carry its own zone -- so a config stat/read/validate here was a
# per-tick gateway stall (the ``no-blocking-call-on-event-loop`` rule), paid
# again by ``get_local_tz`` on the prompt-assembly and dashboard-handler paths.
#
# Ordered by ticket, like the compaction threshold and unlike the alias table:
# this value decides WHEN a job fires, so an out-of-order publish leaving an
# obsolete zone in force would misfire every schedule depending on it until
# something loaded again. The alias table tolerates that race because its
# consequence is a display marker; a schedule does not.
#
# Defaults to "" -- the dataclass default for :attr:`KiroCrewConfig.timezone` --
# so a read taken BEFORE the first load resolves exactly as a defaults-only load
# would (empty falls through to UTC in both consumers) rather than inventing a
# zone during the boot window.
_CONFIG_TIMEZONE: str = ""

_CONFIG_TIMEZONE_TICKET: int = 0

#: Serializes the compare-and-set in :func:`publish_config_timezone`, for the
#: reasons given on :data:`_CONFIG_AUTOCOMPACT_LOCK`: each publish is a read
#: followed by a write, so without it two concurrent loads can both pass the
#: comparison and let whichever assigns last win, rolling the published ticket
#: backwards. A SEPARATE lock rather than reusing the autocompact one, which
#: :func:`next_config_load_ticket` already holds -- drawing a ticket while
#: holding this one is therefore safe, and the reverse nesting must not appear.
#: The READ path (:func:`published_config_timezone`) never takes it, which is
#: what keeps the event loop lock-free.
_CONFIG_TIMEZONE_LOCK = threading.Lock()


def publish_config_timezone(config: "KiroCrewConfig", ticket: int | None = None) -> None:
    """Publish *config*'s default timezone for the filesystem-free read path.

    Pure in-memory rebind -- safe from anywhere, including the event loop, and a
    reader sees either the whole previous value or the whole new one. Called from
    :meth:`KiroCrewConfig.load` so every successful load refreshes it, including
    the degraded-defaults path, which must OVERWRITE a previous snapshot rather
    than leave a zone the files do not name.

    *ticket* orders this publish against concurrent ones and carries the same
    contract as :func:`publish_autocompact_pct`: it must come from
    :func:`next_config_load_ticket`, drawn BEFORE the read that produced
    *config*, and a ticket lower than the one already published is dropped.
    Omitting it draws a fresh ticket, which therefore always wins -- correct for
    a caller publishing a config it just built (tests), wrong for one replaying
    an earlier read.
    """
    global _CONFIG_TIMEZONE, _CONFIG_TIMEZONE_TICKET
    # Drawn OUTSIDE the lock below purely for symmetry with
    # publish_autocompact_pct; next_config_load_ticket takes a DIFFERENT
    # (autocompact) lock, so nesting here would not deadlock as it would there.
    if ticket is None:
        ticket = next_config_load_ticket()
    # Compare and BOTH assignments under one lock: they are a single
    # compare-and-set. See _CONFIG_TIMEZONE_LOCK.
    with _CONFIG_TIMEZONE_LOCK:
        if ticket < _CONFIG_TIMEZONE_TICKET:
            return
        _CONFIG_TIMEZONE_TICKET = ticket
        _CONFIG_TIMEZONE = config.timezone


def published_config_timezone() -> str:
    """The published default timezone name, or ``""`` if none is configured.

    ``""`` is not an error and not a "cold snapshot" signal -- it is what an
    unset :attr:`KiroCrewConfig.timezone` says, and both consumers already
    resolve it to UTC. Callers must NOT treat it as a reason to reach for
    ``config.json`` themselves; that is the I/O this snapshot exists to remove.
    """
    return _CONFIG_TIMEZONE


def resolve_effective_agent(agent_name: str | None, project_dir: str | None = None) -> str:
    """Name the agent that will actually answer *agent_name*, or ``""``.

    A DISPLAY-side companion to :func:`resolve_agent_bindings`, and deliberately
    narrower than it. The empty string means **"nothing to report"** — either the
    requested name is honored, or resolution cannot be settled without touching
    the filesystem. A non-empty return is a positive claim that a DIFFERENT agent
    answers this session -- the default template, as :func:`resolve_agent_bindings`
    binds an unhonored name -- which is what the UI renders as a divergence marker.

    Three properties make it safe to call from ``_ChatSlot.to_dict``, which runs
    on the event loop for every slots frame:

    * **No filesystem access, and no lock.** Only the two in-memory snapshots are
      read (:func:`agent_alias_snapshot` and ``_MATERIALIZED_AGENTS``) plus the
      syscall-free project cache. It never scans, stats, or re-reads
      ``config.json``, so it cannot become a per-frame gateway stall — and because
      the alias snapshot is one immutable tuple, reading it is a single atomic
      name load rather than a mutex acquired once per slot per frame.
    * **Fails closed to "no claim".** A cold alias snapshot, a cold materialized
      snapshot, or a cold project cache all return ``""``. A false
      "your agent was substituted" marker is worse than no marker: the user would
      chase a substitution that never happened, and the honest answer during a
      boot window is silence.
    * **Reads nothing back.** The requested name is never rewritten — see the
      note in ``chat_api/slot_create`` on why storing the resolved name was destructive.
      This function only describes; the stored binding stays verbatim.

    *project_dir* widens the "honored" set to the session's own ``.kiro`` scope
    via the cache-only reader, so a project-declared agent is not mislabelled as
    substituted. A cold cache for that project yields ``""``.
    """
    if not agent_name:
        return ""
    aliases, fallback_template, ready = agent_alias_snapshot()
    if not ready or not fallback_template:
        return ""
    if agent_name in aliases:
        # A Kiro Crew alias resolves to itself (step 1 of resolve_agent_bindings).
        return ""
    if not _MATERIALIZED_AGENTS_READY:
        # Cold snapshot: a materialized kiro agent may well declare this name and
        # we simply cannot see it yet. Claim nothing.
        return ""
    if agent_name in _MATERIALIZED_AGENTS:
        return ""
    if project_dir and not _project_scope_excludes(agent_name, project_dir):
        return ""
    if fallback_template == agent_name:
        return ""
    # What answers is the default TEMPLATE (a template session on the default
    # workspace and Global memory), as resolve_agent_bindings binds it; never a
    # crewmate alias, which would name a store and pin the session does not use.
    return fallback_template


def _project_scope_excludes(agent_name: str, project_dir: str) -> bool:
    """Whether *project_dir* is KNOWN not to declare *agent_name*.

    The conservative half of :func:`_project_declares_agent`: it answers ``True``
    only from a WARM cache, and makes no syscalls even off the event loop. An
    uncached project is not evidence of absence, so it answers ``False`` and the
    caller reports no divergence.
    """
    try:
        # circular import: agent_discovery imports kiro_crew.hooks (the hardened
        # file-read gate), whose import closure reaches back into
        # kiro_crew.config.loader — the same cycle documented at length on
        # :func:`_project_declares_agent`, which defers this identical import for
        # this identical reason. A module-scope import here would be that cycle.
        from kiro_crew.agent_discovery import cached_project_agent_names

        names = cached_project_agent_names(project_dir)
    except Exception:  # noqa: BLE001 — a lookup failure is "no evidence"
        return False
    if names is None:
        return False
    return agent_name not in names


def _read_hardened_agent_spec(path: Path) -> dict | None:
    """Read one agent spec through ``agent_discovery``'s hardened reader.

    Thin wrapper so the model resolvers get the size cap, sensitive-symlink
    rejection, and non-object filtering without each re-deriving them.

    Deferred import so this module keeps its leaf-level import graph —
    ``agent_discovery`` imports ``kiro_crew.hooks``, whose closure reaches
    back into this module (see :func:`_project_declares_agent`). Any failure
    to import or parse means "no usable spec here", never an exception into
    model resolution.
    """
    try:
        from kiro_crew.agent_discovery import _read_agent_spec

        return _read_agent_spec(path, operation="load_config", source="unknown")
    except Exception:
        return None


def _project_declares_agent(agent_name: str, project_dir: str) -> bool:
    """Whether *project_dir* declares a dispatchable agent called *agent_name*.

    Delegates to ``agent_discovery``, which owns the scan, its sensitive-path guards,
    and the stat-signature cache.

    Splits on whether an event loop is running, because that decides what is safe:

    * **Off the loop** (the CLI, tests, and the discovery-pool thread the dashboard
      call sites use) — scan and revalidate normally.
    * **On the loop** — read the cache and nothing else, via a helper that makes no
      syscalls whatsoever. Even one directory's worth of reads is unbounded in
      LATENCY, and this runs on EVERY turn of a project-agent-bound session, so a
      network or otherwise slow checkout would become a recurring gateway stall the
      loop-stall watchdog blames on chat. A cold cache reports "not declared" and the
      caller falls back, exactly as the user-level cold-snapshot path does.

    The dashboard call sites warm the cache through the discovery pool immediately
    before resolving, so the on-loop read is a hit rather than a fallback. Only the
    WARM is offloaded, never ``resolve_agent_bindings`` itself: that function can
    raise ``StopIteration`` on a malformed config, and ``StopIteration`` cannot be
    delivered through a ``Future`` — asyncio rejects it, and the awaiting caller
    hangs instead of seeing the error.

    Deferred import so this module keeps its leaf-level import graph. Best-effort — a
    lookup failure means "not declared here", never an exception into turn handling.
    """
    try:
        # circular import: agent_discovery imports kiro_crew.hooks (the hardened
        # file-read gate), whose import closure reaches back into config.loader —
        # verified by importing agent_discovery in a fresh interpreter and finding
        # kiro_crew.config.loader in sys.modules. A module-scope import here would
        # therefore be a cycle; the deferral is load-bearing, not stylistic.
        from kiro_crew.agent_discovery import (
            cached_project_agent_names,
            project_agent_names,
        )

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return agent_name in project_agent_names(
                project_dir, operation="project_declares_agent", source="unknown"
            )
        names = cached_project_agent_names(project_dir)
        if names is None:
            logger.debug(
                "Project agent cache cold on the event loop for %r; falling back "
                "(warm it off-loop before resolving to dispatch a project agent)",
                agent_name,
            )
            return False
        return agent_name in names
    except Exception:  # noqa: BLE001 — a probe failure only costs a fallback
        logger.debug("Project agent probe failed for %r", agent_name, exc_info=True)
        return False


#: The template kiro-cli runs when ``agent.default_agent`` names none; owned by
#: ``sections`` so the migration module (which must not import this one) reads
#: the same name. Bound here by module attribute, not through the frozen
#: pre-split import list.
DEFAULT_KIRO_TEMPLATE = _sections.DEFAULT_KIRO_TEMPLATE


def default_template(config) -> str:
    """The template a session with no crewmate runs.

    ``agent.default_agent``, else :data:`DEFAULT_KIRO_TEMPLATE`. Deliberately NOT
    the ``default`` crewmate's ``kiro_agent``: a plain session is a template on
    the default folder and memory store, never a crewmate. The
    ``default`` crewmate is one row of ``config.agents`` like any other, talked
    to in its DM thread, and may bind a different template.

    A configured template whose spec is GONE falls back to the managed one, so a
    package uninstall or a hand delete never leaves every new plain session
    refused with "Agent mode ... is not available". "Gone" is a REMOVED name in
    the warm, complete materialized snapshot (:func:`_removal_evidence`), the
    evidence :func:`reset_dangling_default_agent` acts on: absence alone is not
    evidence (a project checkout or the edition can supply a name this directory
    never declared), and a name the edition still supplies is kept. A pure
    in-memory check, so it is safe on the event loop.
    """
    name = getattr(getattr(config, "agent", None), "default_agent", "")
    if not isinstance(name, str) or not name or name == DEFAULT_KIRO_TEMPLATE:
        return DEFAULT_KIRO_TEMPLATE
    names, removed, complete = _removal_evidence()
    if names is not None and complete and name in removed and name not in _edition_agent_names():
        return DEFAULT_KIRO_TEMPLATE
    return name
