"""One-time startup migration: prune the crewmates an older agent sync generated.

An enrol-on-mount build of the dashboard called ``POST /api/agents/sync`` on
every chat mount, and that sync enrolled discovered user and package specs as
crewmates: a ``config.agents`` row with no ``member_id``, on the shared
``default`` memory store, bound to the spec by name and stamped with the spec's
discovery source. An existing install therefore carries one crewmate per synced
user or package agent, most of them never opened.
This module runs once at gateway startup and:

* **removes** each such crewmate the owner never chatted with on the Crewmates
  page -- its DM thread holds no turn -- by deleting its ``config.agents`` row
  (:func:`remove_never_chatted`). The agent stays installed: a session that ran
  it as a subagent, a cron job, an app's own slot or a plain chat used the
  AGENT, not the crewmate, and does not keep the row;
* **leaves the chatted ones exactly as they are**: on the shared ``default``
  store, with no ``member_id``. A memory binding is identity and is chosen only
  at creation; an existing member keeps its exact V1 binding (see
  ``memory-skills-hooks.md``, "Member memory experience and lifecycle"), and no
  startup pass rewrites it.

Design:

* **Precise identification.** A row is a candidate only when it is EXACTLY
  what the sync wrote: its name is its ``kiro_agent``; its string ``source``
  stamp matches an installed spec's discovery source (``package`` and its
  legacy alias ``aim`` are equivalent); and every field it did not copy from
  the spec sits at its default -- no ``member_id``, the shared ``default``
  store, no model, effort, triggers, colour, star, avatar or workspace, and no
  key the record does not declare (:func:`_is_fresh_sync_shape`, tested on the
  RAW row as ``config.json`` holds it, a missing key reading as its default).
  Description is copied from the spec and is not part of that default-field
  comparison, so editing it does not protect a never-chatted generated row.
  The row and spec source must each be ``builtin``, ``package`` or ``aim``;
  ``kirocrew`` (the stamp every non-sync writer leaves), crew private copies,
  and rows without an installed spec are excluded. The one exception to the
  installed-spec test is a row bound to a skill-view alias
  (``kirocrew-skill-view-*``): the runtime writes those files to project a
  spec's skills and discovery never lists them, so the sync's row for one is
  judged on its own stamp and shape. So is every row bound to
  one of the runtime's own agents: a spec discovery marks ``kirocrew_owned``
  (the conductor, worker, knowledge, research and heartbeat specs, which read
  as ``builtin`` because that flag is deliberately kept apart from ``source``
  in ``agent_discovery``) is never a candidate, and a delete is refused when
  the spec re-read under the lock has become one. Both
  config layers are consulted: a name that ``config.local.json`` touches in
  its own ``agents`` section -- ``kirocrew config set --local
  agents.<name>.model``, the capability writer's overlay binding -- is the
  owner's and is never a candidate, because deleting the base row would leave
  the overlay leaf as a crewmate bound to nothing. So is a crewmate that any
  team lists (``crew_teams.read_teams``): placing it on a team is the owner's
  own act, so the row is the owner's whatever its shape. A team document that
  is there but cannot be read keeps every candidate (listed under ``doubted``),
  since none of them can be shown to be off a team. A hand-made crewmate has
  a ``member_id``.
* **Chatted means the DM thread holds a turn.** The one piece of evidence is
  the crewmate's own Crewmates-page thread: its transcript, live or an
  archived segment, has a row past the metadata line (:func:`_chatted`).
  Opening the thread writes a DM binding and at most a metadata line, so a
  crewmate that was only clicked in the roster is not chatted. The binding
  and the transcript are read STRICTLY: one that is there but cannot be read
  or judged keeps that crewmate (listed under ``doubted``), and no
  conversation log at all keeps every candidate. The pass always completes
  and writes the marker; it never loops boot after boot on one bad file, and
  a row it keeps loses nothing by staying.
* **Agent-writable paths are opened defensively.** The DM transcripts are
  opened with ``open_file_no_reparse`` (``O_NOFOLLOW`` / reparse-point refusal
  settled in the same operation as the open, ``O_NONBLOCK`` so a FIFO cannot
  hang the pass) and read only when ``fstat`` says regular file -- a link, a
  FIFO or anything else is not a transcript and is "no record", neither
  evidence nor doubt.
* **One process runs the pass.** The whole pass -- the marker check, the
  history scan, every delete and the marker write -- runs under an exclusive
  cross-process lock on :data:`PRUNE_LOCK` beside the marker
  (``platform_compat.file_lock``), and the marker itself is created
  ``O_EXCL``. A second gateway on the same data home therefore cannot run its
  own pass beside this one: its pass waits on the lock -- and its own request
  barrier holds its writers for as long as it waits -- then finds the marker
  and returns. The wait has no give-up: each :data:`PRUNE_LOCK_WAIT_S` that
  passes logs one WARNING and tries again, because a contender that returned
  while the holder still held the lock would settle its own barrier and let a
  session bind a crewmate the holder had not judged yet, and the holder would
  then delete a row that is in use. A holder that dies releases the lock
  with its process, so the wait ends. A process-local event alone would let
  that second process bind a session to a candidate between this process's
  history scan and its delete.
* **Serialized against every session-agent writer, off the boot path.** The
  gateway clears ``DashboardState.crewmate_prune_settled`` BEFORE the listener
  binds (``_register_crewmate_prune_gate``); the pass itself is kicked as a
  tracked background task right after the bind (``_kick_crewmate_prune``) and
  sets the event in ``finally``. Nothing on the startup path awaits it, so
  readiness is never gated by a scan whose cost scales with the session
  count; the slot restores need not wait either: a removed row's DM thread
  held no turn, and a session that ran its agent elsewhere resolves the same
  name onto the installed agent on the default crew's workspace and memory --
  the binding the removed row carried -- so no restore depends on the row.
  A session with no execution record gets that from the installed-agent
  lookup in ``resolve_agent_bindings``. One whose record names the crewmate as
  a ``member`` selection does not, because a member selection never takes that
  lookup; ``execution_context.adopt_removed_synced_crewmate`` re-reads exactly
  the shape this pass removes (the shared store, named after its agent, and
  listed in this pass's marker) as that agent when the record is decoded, so
  every reader agrees. Every other missing member stays refused.
  That
  function's middleware holds every mutating request on it -- the chat send,
  slot create, slot agent switch, member thread, channel and import routes
  under ``/api/`` and the OpenAI-compatible ``POST /v1/chat/completions`` are
  all such requests -- so no session can bind an agent, and no DM binding can
  appear, between a candidate's check and its delete. It also holds every
  request under ``/api/members`` whatever its method, so the roster is read
  after the pass has settled rather than beside a delete. The writers that do
  not come through HTTP -- the subagent pump, channel agent resume, cron
  dispatch -- start only after ``await_crewmate_prune_settled`` returns (in
  ``GatewayOrchestrator.run`` after the memory barrier, past
  ``KIROCREW_READY``; in the standalone dashboard before its inline channel
  resume), and that helper returns only once the pass has RETURNED: when the
  pass outlives its budget the helper sets the abandon flag -- read here
  before each candidate and again inside the config lock before each delete
  -- and the pass keeps whatever it has not judged (listed under ``doubted``),
  writes its marker and returns; no writer ever runs beside a pass that can
  still delete. Each candidate's check runs immediately before its own
  removal, never once for the whole list.
* **A refused delete is not a commit.** The delete re-tests the row inside
  the base config lock: the base row must still carry the same ``kiro_agent``,
  source identity and fresh-sync shape; for a row bound to an installed spec,
  that spec is re-read from disk
  under ``agents_spec_lock`` (nested inside the config lock, the order every
  other spec writer keeps) and must still declare the bound name, remain
  non-private and not ``kirocrew_owned``, and have the same canonical
  discovery source; and the overlay must
  still not name it,
  read under its own sidecar lock, taken inside the base lock and held until
  the base write has committed, so no overlay leaf can land for the name
  between that check and the delete; and no team may list it, the team
  document re-read under ``crew_teams.document_lock`` -- taken last, inside
  the spec lock, and held the same way, so no team write can place the name
  between that check and the delete. A row that changed meanwhile in any of
  these ways is refused, the pass writes no marker and logs which rows, and
  the next boot re-judges them.
* **Agent files are never touched.** Only ``config.json`` rows move. The specs
  under ``~/.kiro/agents`` are only read, and any transcript on disk stays
  exactly as it is.
* **Idempotent, marker-gated.** A completed pass (even a no-op) writes
  :data:`PRUNE_MARKER` under the config directory with what it did -- the same
  marker-file seam the config loader's own one-shot migrations use
  (``CONNECTIONS_UI_MIGRATION_MARKER``); the next boot finds the marker and
  returns at once. It runs from ``start_dashboard`` rather than inside the
  loader because the decision needs chat history, which only the running
  gateway has. Removed rows do not come back: nothing in the dashboard calls
  ``POST /api/agents/sync`` (``useAgents`` reads the catalog), so the rows this
  pass removes come only from installs that ran an enrol-on-mount build.
"""

from __future__ import annotations

import contextlib
import dataclasses
import errno
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable
from pathlib import Path

from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.config.loader import (
    ConfigReadError,
    KiroCrewAgentConfig,
    KiroCrewConfig,
    _config_write_lock,
    _lock_target,
    coerce_dict_section,
    config_local_path,
    read_config_for_update,
    update_config_locked,
)
from kiro_crew.config.paths import config_dir
from kiro_crew.crew_teams import TeamsUnreadable, document_lock, read_teams
from kiro_crew.jsonl_util import OversizedRecord, UnreadableRecord, strict_raw_records
from kiro_crew.platform_compat import file_lock, open_file_no_reparse, open_lock_file

logger = logging.getLogger(__name__)

#: The exact name the skill projection writes for an alias: the reserved
#: prefix plus the first 24 hex digits of its digest. A row is judged as an
#: alias row only on a full match, so a crewmate someone named with the prefix
#: but any other tail is judged as an ordinary row and needs an installed spec.
_SKILL_VIEW_ALIAS_NAME_RE = re.compile(re.escape(NATIVE_SKILL_ALIAS_PREFIX) + r"[0-9a-f]{24}")


def _is_skill_view_alias_row(name: str) -> bool:
    """Whether *name* is exactly a projection-written skill-view alias name."""
    return _SKILL_VIEW_ALIAS_NAME_RE.fullmatch(name) is not None


#: Written under the config directory once a pass completes. Its body is the
#: record of what the pass did, so an operator can see which crewmates left
#: and which were kept because their history could not be read. Earlier
#: markers (``crewmate_prune_migrated.json``, ``crewmate_prune_v2_migrated.json``)
#: are left in place: the passes that wrote them judged a narrower set of rows,
#: so a new marker name lets the current pass run once on those installs too.
PRUNE_MARKER = "crewmate_prune_v3_migrated.json"

#: The cross-process lock the whole pass runs under, beside the marker. Two
#: gateway processes on one data home take turns here; the second finds the
#: first's marker and returns.
PRUNE_LOCK = "crewmate_prune.lock"

#: How long one acquire of :data:`PRUNE_LOCK` waits before the pass logs that
#: it is still waiting and tries again. Not a give-up: a pass never returns
#: while another process holds the lock (see the module docstring), it only
#: says so this often. Longer than the gateway's own writer budget for the
#: pass (60 s), so the first line is not written for a holder that is merely
#: slow.
PRUNE_LOCK_WAIT_S = 120.0

#: The ``source`` stamps a sync-written row can carry. The sync copied the
#: bound spec's discovery source onto the row: ``builtin`` for a spec the user
#: wrote and ``package`` for one a package installed (``aim`` is that source's
#: older name).
SYNC_ROW_SOURCES = frozenset({"builtin", "package", "aim"})


class HistoryUnreadable(RuntimeError):
    """A piece of chat history could not be read.

    Raised by the strict readers below and caught by :func:`prune_synced_crewmates`,
    which keeps the crewmates the unreadable piece could have vouched for.
    """


@dataclasses.dataclass
class PruneReport:
    removed: list[str] = dataclasses.field(default_factory=list)
    kept: list[str] = dataclasses.field(default_factory=list)
    #: Kept because history could not be read in full; reason per name.
    doubted: dict[str, str] = dataclasses.field(default_factory=dict)
    refused: list[str] = dataclasses.field(default_factory=list)
    skipped_marker: bool = False
    #: How many :data:`PRUNE_LOCK_WAIT_S` waits passed with another process
    #: holding :data:`PRUNE_LOCK` before this pass got it (or found the marker).
    lock_waits: int = 0


@dataclasses.dataclass(frozen=True)
class SyncedCandidate:
    """What the discovery snapshot recorded about one candidate's spec.

    ``filename`` is the spec file under the agents directory, so the delete can
    re-read that same file under the spec lock and confirm it remains readable.
    ``source`` is the canonical discovery source the row matched, so a source
    change between discovery and deletion refuses the delete. ``skill_view`` marks
    a row bound to a skill-view alias (``kirocrew-skill-view-*``): discovery
    never lists an alias, so such a row has no spec to match and ``filename`` is
    empty; its ``source`` is the row's own stamp.
    """

    filename: str
    source: str
    skill_view: bool = False


def marker_path() -> Path:
    return config_dir() / PRUNE_MARKER


#: Every marker a prune pass has written, oldest first. Each body's ``removed``
#: list names rows that pass deleted.
_ALL_MARKERS = ("crewmate_prune_migrated.json", "crewmate_prune_v2_migrated.json", PRUNE_MARKER)

#: A marker is a short list of names; anything larger is not one this module wrote.
_MARKER_READ_CAP = 1 << 20

_removed_cache: tuple[tuple, frozenset[str]] | None = None


def removed_crewmate_names() -> frozenset[str]:
    """The ``config.agents`` names any prune pass recorded as removed.

    This is the provenance :func:`kiro_crew.execution_context.adopt_removed_synced_crewmate`
    keys on: a record is re-read as its template only when the row it names is
    one THIS migration deleted, never because some member row happens to be
    absent. An unreadable or malformed marker contributes nothing. Cached on
    each marker's size and mtime, so the hot decode path costs a few ``stat``
    calls.
    """
    global _removed_cache
    root = config_dir()
    paths = [root / name for name in _ALL_MARKERS]
    stamps: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            info = path.stat()
        except OSError:
            continue
        stamps.append((path.name, info.st_mtime_ns, info.st_size))
    key = tuple(stamps)
    cached = _removed_cache
    if cached is not None and cached[0] == key:
        return cached[1]
    names: set[str] = set()
    for name, _mtime, size in stamps:
        if size > _MARKER_READ_CAP:
            continue
        try:
            body = json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            continue
        removed = body.get("removed") if isinstance(body, dict) else None
        if isinstance(removed, list):
            names.update(n for n in removed if isinstance(n, str) and n)
    result = frozenset(names)
    _removed_cache = (key, result)
    return result


def lock_path() -> Path:
    return config_dir() / PRUNE_LOCK


def _raw_agents_section(path: Path | None = None) -> dict:
    """The ``agents`` section exactly as one config file holds it.

    ``path`` defaults to ``config.json``; pass :func:`config_local_path` for the
    overlay. A file that is absent reads as an empty section; one that is
    present but unreadable raises ``ConfigReadError`` (fail closed: the pass
    cannot judge a layer it cannot see).
    """
    doc = read_config_for_update(path)
    agents = doc.get("agents") if isinstance(doc, dict) else None
    return agents if isinstance(agents, dict) else {}


#: Sync copies these fields from the spec rather than using record defaults.
_SPEC_COPIED_FIELDS = frozenset({"description", "source"})


def _canonical_sync_source(source: str) -> str:
    """Return the canonical source identity for sync row and spec comparison."""
    return "package" if source in {"package", "aim"} else source


def _is_fresh_sync_shape(raw: dict, *, kiro_agent: str) -> bool:
    """Whether a RAW ``config.agents`` row is exactly a sync-written row.

    Compared field by field against a default record bound to ``kiro_agent``.
    ``description`` and ``source`` are copied from the spec and handled
    separately; every other declared field must equal its default. The row may
    carry no key the record does not declare. A declared key the row lacks
    reads as its default, so a row written by a build whose record had fewer
    fields is still the sync's row.
    """
    expected = dataclasses.asdict(KiroCrewAgentConfig(kiro_agent=kiro_agent))
    if set(raw) - set(expected):
        return False
    for key, default in expected.items():
        if key in _SPEC_COPIED_FIELDS:
            continue
        if raw.get(key, default) != default:
            return False
    return True


def _teamed_names() -> frozenset[str]:
    """Every crewmate name some team lists, as ``crew-teams/teams.json`` holds it.

    An absent document is no teams. One that is there but cannot be read raises
    :class:`~kiro_crew.crew_teams.TeamsUnreadable` (``read_teams`` never answers
    an empty list for it); the caller keeps every candidate on that doubt. The
    names are taken as written, not filtered against the registry: a candidate
    is a registry name, so a stale entry for a gone crew names nothing here.
    """
    return frozenset(member for team in read_teams() for member in team.members)


def _synced_candidates(
    cfg: KiroCrewConfig, raw_agents: dict, overlay_agents: dict, teamed: frozenset[str]
) -> dict[str, SyncedCandidate]:
    """The crewmates an older sync generated, in config order, each with what
    discovery recorded about the spec it is bound to (see :class:`SyncedCandidate`).

    A row's string source must match the installed spec's discovery source;
    ``package`` and ``aim`` compare as one source identity. Specs and rows use
    only ``builtin``, ``package`` or ``aim``. Rows without an installed spec,
    crew-private specs and the runtime's own specs (``kirocrew_owned``, which
    discovery keeps apart from ``source``: the conductor, worker, knowledge,
    research and heartbeat specs read as ``builtin``) are excluded -- a row
    bound to one of the runtime's own agents is never pruned. ``raw_agents`` is
    the ``agents`` section as ``config.json`` holds it (not the default-filled
    dataclasses): the shape test must see the row the file holds, and the same
    test is re-run inside the delete's lock. ``overlay_agents`` is the same
    section from ``config.local.json``; any name it mentions is excluded,
    whatever it says about it. ``teamed`` is every name some team lists
    (:func:`_teamed_names`); a teamed crewmate is the owner's and is excluded.

    A row bound to a skill-view alias -- exactly the name the projection
    writes, the prefix plus 24 lowercase hex digits -- is judged on the row
    alone; a prefixed name with any other tail is an ordinary row. The alias is a file the runtime writes to project one spec's
    skills, never an agent a person installs or names, and discovery leaves it
    out of the roster -- so an older sync that walked the agents directory
    before that exclusion enrolled one crewmate per alias, and no installed spec
    will ever match one. Its string stamp must still be a sync source and its
    shape a fresh sync row.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import list_agents

    specs = {info.name: info for info in list_agents(agents_dir=kiro_agents_dir_path())}
    out: dict[str, SyncedCandidate] = {}
    for name, agent in cfg.agents.items():
        if name in ("default", cfg.default_agent):
            continue
        if name in overlay_agents or name in teamed:
            continue
        raw = raw_agents.get(name)
        if not isinstance(raw, dict) or name != agent.kiro_agent:
            continue
        if _is_skill_view_alias_row(agent.kiro_agent):
            alias_source = raw.get("source")
            if (
                isinstance(alias_source, str)
                and alias_source in SYNC_ROW_SOURCES
                and _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent)
            ):
                out[name] = SyncedCandidate(
                    filename="", source=_canonical_sync_source(alias_source), skill_view=True
                )
            continue
        spec = specs.get(agent.kiro_agent)
        if spec is None or spec.private_to or spec.kirocrew_owned or not spec.filename:
            continue
        raw_source = raw.get("source")
        spec_source = spec.source
        if not isinstance(raw_source, str) or spec_source not in SYNC_ROW_SOURCES:
            continue
        if not _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent):
            continue
        source = _canonical_sync_source(spec_source)
        if _canonical_sync_source(raw_source) != source:
            continue
        out[name] = SyncedCandidate(filename=spec.filename, source=source)
    return out


def _read_discovered_spec_identity(filename: str) -> tuple[str, str, str, bool] | None:
    """Return the spec's current ``(name, source, private_to, kirocrew_owned)``.

    The file goes through :func:`read_agent_spec_strict`, then the same
    :func:`_global_agent_info` derivation and fork-lineage lookup as global
    discovery -- ``kirocrew_owned`` included, which that derivation reads off
    the filename (``OWNED_KIRO_AGENT_FILES``) and never off ``source``. A
    missing, unreadable, refused, non-record or unclassifiable spec fails
    closed. The file and lineage are only read, never written.
    """
    from kiro_crew import agent_state
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import _global_agent_info, read_agent_spec_strict

    path = Path(kiro_agents_dir_path()) / filename
    try:
        data = read_agent_spec_strict(path, operation="crewmate_prune", source="dashboard")
        if not isinstance(data, dict):
            return None
        info = _global_agent_info(path, data)
        fork_info = agent_state.get_fork_info(info.name, strict=True)
    except (OSError, ValueError):
        return None
    private_to = fork_info["private_to"] if fork_info else ""
    return info.name, info.source, private_to, info.kirocrew_owned


#: Longest first line the DM transcript read accepts. The first line is the
#: metadata record, a few hundred bytes in practice; the cap bounds the cost of
#: one read on an agent-writable tree.
_TRANSCRIPT_META_LINE_MAX = 64 * 1024


def _transcript_has_a_turn(path: Path, *, missing_is_doubt: bool = False) -> bool:
    """Whether one transcript file holds anything past its metadata line.

    A transcript is born with its metadata record and gains one row per
    message, so a second non-blank line means a turn was written; its content
    is not parsed, since even a torn row proves one was being written. A first
    line that parses to something other than the metadata record is a message
    row from an older build, and counts the same way.

    ``False`` when the file does not exist (unless ``missing_is_doubt``), is a
    link, a FIFO or anything but a regular file (opened with
    ``open_file_no_reparse``, ``O_NONBLOCK``: the link refusal lands in the
    open, and a FIFO returns at once instead of hanging), or holds only its
    metadata line. Raises :class:`HistoryUnreadable` when the file is there but
    cannot be judged: an open or read failure, an empty file (a torn write), or
    a first line that is over budget, not UTF-8 or not JSON. The caller keeps
    the crewmate. ``missing_is_doubt`` is for an archived segment already seen
    in a directory listing: if it disappears before the open, its evidence is
    unknown rather than absent.
    """
    try:
        fd = open_file_no_reparse(path, nonblocking=True)
    except FileNotFoundError as exc:
        if missing_is_doubt:
            raise HistoryUnreadable(
                f"transcript {path.name} disappeared before it could be opened"
            ) from exc
        return False
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return False
        raise HistoryUnreadable(f"transcript {path.name} could not be opened: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            first = fh.readline(_TRANSCRIPT_META_LINE_MAX + 1)
            if len(first) > _TRANSCRIPT_META_LINE_MAX:
                raise HistoryUnreadable(
                    f"transcript {path.name}: first line exceeds {_TRANSCRIPT_META_LINE_MAX} bytes"
                )
            try:
                text = first.decode("utf-8").strip()
            except UnicodeError as exc:
                raise HistoryUnreadable(f"transcript {path.name} is not UTF-8: {exc}") from exc
            if not text:
                raise HistoryUnreadable(f"transcript {path.name} is empty: no metadata line")
            try:
                record = json.loads(text)
            except ValueError as exc:
                raise HistoryUnreadable(
                    f"transcript {path.name}: first line is not JSON: {exc}"
                ) from exc
            if not isinstance(record, dict) or record.get("_type") != "metadata":
                return True
            try:
                for line in strict_raw_records(fh, path, cap=_TRANSCRIPT_META_LINE_MAX):
                    if line.strip():
                        return True
            except OversizedRecord:
                # A row too long to hold is still a row: a turn was written.
                return True
            except UnreadableRecord as exc:
                raise HistoryUnreadable(f"transcript {path.name} could not be read: {exc}") from exc
            return False
    except OSError as exc:
        raise HistoryUnreadable(f"transcript {path.name} could not be read: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def _dm_binding_slot_key(name: str, slug: str) -> str:
    """The slot key this crewmate's DM binding records, or ``""`` for none.

    Read STRICTLY, not through ``read_dm_binding``: that reader is total by
    contract and answers "not bound" for an unreadable file or a malformed
    payload alike, which a removal must never mistake for "never opened". Only
    a binding file that does not exist reads as none; any other failure raises
    :class:`HistoryUnreadable`. A binding that names another crew (slugs
    collide) is not this crewmate's thread and reads as none.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.atomic_write import read_bytes_with_retry

    try:
        path = members_mod.dm_binding_path(slug)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable path is "unknown", never "no"
        # ``MemberSlugError`` included: the slug passed ``member_slug`` already,
        # so here it means the containment check refused a path that resolves
        # outside the trust root -- a binding that may exist, not one that does not.
        raise HistoryUnreadable(f"could not resolve {name!r}'s DM binding: {exc}") from exc
    try:
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return ""
    except Exception as exc:  # noqa: BLE001 -- present but unreadable is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s DM binding: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HistoryUnreadable(f"{name!r}'s DM binding does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise HistoryUnreadable(f"{name!r}'s DM binding is not a record")
    if data.get("member") != name:
        return ""
    slot_key = data.get("slot_key")
    return slot_key if isinstance(slot_key, str) else ""


def _chatted(cfg: KiroCrewConfig, name: str, sessions_dir: Path) -> bool:
    """Whether the owner ever chatted with this crewmate on the Crewmates page.

    The one piece of evidence is the crewmate's own DM thread holding a turn:
    its transcript, live or an archived segment of it, has a row past the
    metadata line (:func:`_transcript_has_a_turn`). Nothing else counts. A
    session elsewhere that ran as this agent -- a subagent spawn, a cron job,
    an app's own slot, a plain chat that picked the template -- used the
    AGENT, not the crewmate, and the agent stays installed after the row goes.
    Opening the thread without sending anything writes the DM binding and at
    most a metadata line, so a crewmate that was only clicked in the roster is
    not chatted either.

    The thread's transcript lives under ``dashboard_<slot key>``. The slot key
    is derived from the slug (:func:`~kiro_crew.members.member_slot_key`), and
    the DM binding's recorded key is read too, so a thread opened under another
    derivation is not missed. A binding that cannot be read, or a transcript
    that is there but cannot be judged, raises :class:`HistoryUnreadable` and
    the caller keeps the crewmate.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.history import ARCHIVE_DIR_NAME, ARCHIVE_SEGMENT_DELIMITER, _safe_key

    try:
        slug = members_mod.member_slug(name, cfg)
    except members_mod.MemberSlugError:
        # No slug means no DM thread can exist.
        return False
    slot_keys = {members_mod.member_slot_key(slug)}
    bound = _dm_binding_slot_key(name, slug)
    if bound:
        slot_keys.add(bound)
    stems: list[str] = []
    for slot_key in sorted(slot_keys):
        stem = _safe_key(f"dashboard_{slot_key}")
        if _transcript_has_a_turn(sessions_dir / f"{stem}.jsonl"):
            return True
        stems.append(stem)

    archive = sessions_dir / ARCHIVE_DIR_NAME
    prefixes = tuple(f"{stem}{ARCHIVE_SEGMENT_DELIMITER}" for stem in stems)
    try:
        with os.scandir(archive) as entries:
            archive_names = sorted(
                entry.name
                for entry in entries
                if entry.name.endswith(".jsonl") and entry.name.startswith(prefixes)
            )
    except FileNotFoundError:
        archive_names = []
    except OSError as exc:
        raise HistoryUnreadable(f"could not list archived transcripts: {exc}") from exc
    for stem in stems:
        prefix = f"{stem}{ARCHIVE_SEGMENT_DELIMITER}"
        for name in archive_names:
            if name.startswith(prefix) and _transcript_has_a_turn(
                archive / name, missing_is_doubt=True
            ):
                return True
    return False


def _never_abandoned() -> bool:
    return False


def remove_never_chatted(
    cfg: KiroCrewConfig,
    candidates: dict[str, SyncedCandidate],
    *,
    abandoned: Callable[[], bool] = _never_abandoned,
) -> tuple[list[str], list[str], list[str]]:
    """Delete the ``config.agents`` rows named; returns ``(removed, refused, abandoned)``.

    ``candidates`` maps each name to the spec filename and canonical source
    identity :func:`_synced_candidates` recorded. Each delete re-runs the
    candidate test on the rows and spec as the files hold them, inside the base
    config lock: the base row must carry the same ``kiro_agent``, source
    identity and fresh-sync shape (:func:`_is_fresh_sync_shape`);
    ``config.local.json`` must still not name it, read under the overlay's own
    sidecar lock; for a row bound to an installed spec, that spec, re-read from
    disk under ``agents_spec_lock``, must still declare ``kiro_agent``, remain
    non-private, not be one of the runtime's own (``kirocrew_owned``) and have
    the same canonical discovery source (a skill-view alias row has no spec, so
    its exact alias name and row shape are the whole test); and no team may list it, the team document
    re-read under ``crew_teams.document_lock``. The three inner locks are
    taken inside the base lock, overlay then spec then team document -- the
    first two in the order every binding writer keeps, the team lock last
    because its own contract is "the registry's lock first, then this one" and
    nothing takes a registry, overlay or spec lock while holding it -- and held
    until the base write has committed, so neither an overlay writer landing a
    leaf for the name, a spec writer replacing the file nor a team write placing
    the name can slip between the check and the delete. A row or spec that
    changed meanwhile
    -- the row's identity or shape changed, a member-aware write stamped it, the
    spec vanished, stopped reading as a spec, changed identity, became private
    or became one of the runtime's own, an overlay leaf appeared, or a team
    lists the name -- is newer
    evidence and is refused, not deleted.
    The test is on identity and shape, never on equality with a default-filled
    snapshot: a row written by a build whose record had fewer keys must still be
    recognised as the sync's. Nothing but the base row moves: the overlay, the
    spec under ``~/.kiro/agents`` (only read) and any transcript stay.

    ``abandoned`` is read inside the config lock, after every re-check and
    right before the delete: once it answers true the row is left in place and
    named in the third list, and so is every row after it. The gateway sets it
    when the pass outlives its budget and then waits for the pass to return
    before any session writer starts, so the delete this check guards is the
    last thing that could still remove a row a new session is about to name.

    Callers hold :data:`PRUNE_LOCK` (:func:`prune_synced_crewmates` does); the
    config lock taken here serializes the row write itself, not the pass.
    """
    from kiro_crew.agent import agents_spec_lock, kiro_agents_dir_path

    removed: list[str] = []
    refused: list[str] = []
    left: list[str] = []
    overlay_path = _lock_target(config_local_path())
    for name, candidate in candidates.items():
        if abandoned():
            left.append(name)
            continue
        kiro_agent = cfg.agents[name].kiro_agent
        deleted = False
        gave_up = False

        # The overlay's sidecar lock and the spec lock are entered on this
        # stack from inside ``_mutate`` and so outlive the callback: both are
        # released only after ``update_config_locked`` has renamed the base
        # file into place. A lock released when its check returned would leave
        # the window the check exists to close -- an overlay writer landing a
        # leaf for the name, or a spec writer replacing the file, after the
        # check and before the base row is gone.
        with contextlib.ExitStack() as locks:

            def _mutate(
                doc: dict,
                _name: str = name,
                _bound: str = kiro_agent,
                _file: str = candidate.filename,
                _source: str = candidate.source,
                _skill_view: bool = candidate.skill_view,
                _locks: contextlib.ExitStack = locks,
            ) -> dict | None:
                nonlocal deleted, gave_up
                agents = coerce_dict_section(doc, "agents")
                raw = agents.get(_name)
                if not isinstance(raw, dict) or raw.get("kiro_agent") != _bound:
                    return None
                if not _is_fresh_sync_shape(raw, kiro_agent=_bound):
                    return None
                raw_source = raw.get("source")
                if not isinstance(raw_source, str):
                    return None
                if _canonical_sync_source(raw_source) != _source:
                    return None
                # Base lock first, overlay lock second, spec lock third -- the
                # order every binding writer keeps (``_write_bindings``, the
                # template create) -- and the team document lock fourth: its
                # own contract is registry lock first, then it, and nothing
                # takes a registry, overlay or spec lock while holding it, so
                # entering it innermost cannot invert any order. All three
                # inner locks are entered on the stack that outlives this
                # callback, so they are released only after
                # ``update_config_locked`` has renamed the base file into
                # place: neither an overlay leaf for the name, a replacement
                # of the spec nor a team write placing the name can land
                # between the checks below and the delete. The overlay and the
                # team document are read directly under their locks and the
                # spec is re-read under its lock; nothing is written to any of
                # them. A lock that cannot be taken, an unreadable overlay or
                # team document, or a spec whose discovery identity differs
                # from the candidate is doubt, and doubt refuses.
                try:
                    _locks.enter_context(_config_write_lock(overlay_path))
                    overlay = read_config_for_update(overlay_path)
                    _locks.enter_context(agents_spec_lock(Path(kiro_agents_dir_path())))
                    _locks.enter_context(document_lock())
                    teamed = _teamed_names()
                except (OSError, ConfigReadError, TeamsUnreadable):
                    return None
                if _name in teamed:
                    return None
                if _skill_view:
                    # No spec to re-read: discovery never lists an alias. The
                    # row's own identity and shape, checked above, are the
                    # whole test.
                    if not _is_skill_view_alias_row(_bound):
                        return None
                else:
                    spec_identity = _read_discovered_spec_identity(_file)
                    if spec_identity is None:
                        return None
                    spec_name, spec_source, private_to, kirocrew_owned = spec_identity
                    if (
                        spec_name != _bound
                        or private_to
                        or kirocrew_owned
                        or _canonical_sync_source(spec_source) != _source
                    ):
                        return None
                overlay_agents = overlay.get("agents")
                if isinstance(overlay_agents, dict) and _name in overlay_agents:
                    return None
                # Last check before the delete, under the locks the delete
                # holds: a pass told to stop removes nothing further, whatever
                # it had judged.
                if abandoned():
                    gave_up = True
                    return None
                del agents[_name]
                deleted = True
                return doc

            update_config_locked(mutate=_mutate)
        if deleted:
            removed.append(name)
            del cfg.agents[name]
        elif gave_up:
            left.append(name)
        else:
            refused.append(name)
    return removed, refused, left


def _write_marker(report: PruneReport) -> None:
    """Create the marker ``O_EXCL`` and write the pass's record into it.

    Exclusive creation, not replace: the marker is the claim that ONE pass
    completed, so a marker already there -- another process's, written while
    this one waited on the lock -- is left as it is and ``FileExistsError``
    propagates; the caller reads it as that other pass having settled the
    question. The bytes are flushed to disk before the close, so a marker that
    exists is one whose pass returned.
    """
    body = {
        "migrated_at": time.time(),
        "removed": report.removed,
        "kept": report.kept,
        "doubted": report.doubted,
    }
    marker = marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(os.fspath(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(json.dumps(body, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if fd >= 0:
            os.close(fd)


#: The ``doubted`` reason for a candidate the pass was told to stop before judging.
ABANDONED_REASON = "the startup barrier timed out before this crewmate was judged"

#: The ``doubted`` reason when ``crew-teams/teams.json`` is there but cannot be
#: read: no candidate can then be shown to be off a team.
TEAMS_UNREADABLE_REASON = "the crew-teams document could not be read; team membership unknown"


def prune_synced_crewmates(
    conversation_log, *, abandoned: Callable[[], bool] = _never_abandoned
) -> PruneReport:
    """Run the pass once. Thread-side; safe to call on every boot.

    ``conversation_log`` is the gateway's :class:`~kiro_crew.history.ConversationLog`;
    ``None`` means history is unavailable, so every candidate is kept on doubt,
    and so does a ``crew-teams/teams.json`` that is there but cannot be read.
    Never raises :class:`HistoryUnreadable` or
    :class:`~kiro_crew.crew_teams.TeamsUnreadable`: unreadable evidence keeps
    the crewmates it could have vouched for, and the pass still finishes.

    The whole pass runs under an exclusive cross-process lock on
    :data:`PRUNE_LOCK`: the marker check, the history scan, each delete and
    the marker write. A second gateway on the same data home waits here --
    its own request barrier holds its writers for the whole wait -- and then
    finds the marker. The wait never gives up: every :data:`PRUNE_LOCK_WAIT_S`
    it logs one WARNING (counted in ``lock_waits``) and tries again, since a
    pass that returned with the lock still held would open its barrier while
    the holder can still delete. A holder's death releases the lock. Between
    tries the marker is checked without the lock: it is written last, under
    the lock, so its presence means every delete is done. Blocking; never
    call on the event loop thread (``file_lock`` refuses to poll there).

    ``abandoned`` is polled before each candidate and inside the config lock
    before each delete (:func:`remove_never_chatted`). Once it answers true
    every candidate not yet removed is kept and listed under ``doubted`` with
    :data:`ABANDONED_REASON`, and the marker is still written: the rows lose
    nothing by staying, and a pass that re-ran every boot would hold the
    gateway's writers every boot. The gateway sets it when the pass outlives
    its startup budget.
    """
    report = PruneReport()
    lock = lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open_lock_file(lock) as lock_fd, contextlib.ExitStack() as held:
        # The acquire is tried on its own so that only IT can read as "another
        # process holds the pass": a failure inside the pass proper propagates
        # as itself, never mislabelled as contention.
        while True:
            try:
                held.enter_context(file_lock(lock_fd, exclusive=True, timeout=PRUNE_LOCK_WAIT_S))
                break
            except OSError as exc:
                # Held by another process. That process owns the pass; this
                # one must not return while it can still delete, because the
                # caller settles its request barrier on return and a session
                # bound here would be invisible to the holder's history scan.
                # So: wait again. The marker is written last, under the lock,
                # so finding it means the holder's deletes are all done.
                report.lock_waits += 1
                if marker_path().exists():
                    report.skipped_marker = True
                    return report
                logger.warning(
                    "crewmate prune still waiting: another process holds %s (%s); "
                    "session writers stay held until it finishes",
                    lock.name,
                    exc,
                )
        return _prune_locked(conversation_log, report, abandoned=abandoned)


def _judge_each(
    cfg: KiroCrewConfig,
    candidates: dict[str, SyncedCandidate],
    sessions_dir: Path,
    report: PruneReport,
    *,
    abandoned: Callable[[], bool],
) -> None:
    """Judge and, when never chatted, remove each candidate in turn."""
    # Check and delete ONE candidate at a time: the strict history check
    # runs immediately before its own row's removal, never once for the
    # whole list up front. The gateway holds every mutating request
    # back while the pass runs (``DashboardState.crewmate_prune_settled``,
    # armed before the listener bound), so no session can bind an agent and
    # no thread can be opened between a candidate's check and its delete.
    for name, candidate in candidates.items():
        if abandoned():
            report.doubted[name] = ABANDONED_REASON
            continue
        try:
            chatted = _chatted(cfg, name, sessions_dir)
        except HistoryUnreadable as exc:
            report.doubted[name] = str(exc)
            continue
        if chatted:
            report.kept.append(name)
        else:
            removed, refused, left = remove_never_chatted(
                cfg, {name: candidate}, abandoned=abandoned
            )
            report.removed.extend(removed)
            report.refused.extend(refused)
            for gone in left:
                report.doubted[gone] = ABANDONED_REASON


def _prune_locked(
    conversation_log, report: PruneReport, *, abandoned: Callable[[], bool]
) -> PruneReport:
    """The pass proper; the caller holds :data:`PRUNE_LOCK`."""
    marker = marker_path()
    if marker.exists():
        report.skipped_marker = True
        return report
    cfg = KiroCrewConfig.load()
    raw_agents = _raw_agents_section()
    overlay_agents = _raw_agents_section(config_local_path())
    # An unreadable team document is doubt about EVERY candidate, the way no
    # conversation log is: none can be shown to be off a team. Read as "no
    # teams" only for the candidate test below, so the doubt can name the rows
    # it keeps; nothing is judged or removed on it.
    teams_doubt = ""
    try:
        teamed = _teamed_names()
    except TeamsUnreadable as exc:
        teams_doubt = f"{TEAMS_UNREADABLE_REASON} ({exc})"
        teamed = frozenset()
    candidates = _synced_candidates(cfg, raw_agents, overlay_agents, teamed)
    if candidates:
        sessions_dir = getattr(conversation_log, "_dir", None)
        if teams_doubt:
            for name in candidates:
                report.doubted[name] = teams_doubt
        elif not isinstance(sessions_dir, Path):
            # No transcripts to read: nothing can be shown never to have been
            # chatted with, so everyone is kept. The marker still records it.
            for name in candidates:
                report.doubted[name] = "no conversation log; removal needs chat history"
        else:
            _judge_each(cfg, candidates, sessions_dir, report, abandoned=abandoned)
    if report.doubted:
        logger.warning(
            "crewmate prune: %d crewmate(s) kept because their history could not be read: %s",
            len(report.doubted),
            "; ".join(f"{name}: {why}" for name, why in report.doubted.items()),
        )
    if report.refused:
        # A refused delete is not a commit: the row on disk was not the row
        # judged. Nothing is recorded as done; the next boot re-judges it.
        logger.warning(
            "crewmate prune: %d row(s) changed while being judged, pass not recorded: %s",
            len(report.refused),
            ", ".join(report.refused),
        )
        return report
    try:
        _write_marker(report)
    except FileExistsError:
        # Cannot happen while this process holds the lock; if it does, the
        # other marker is the record and this pass is already accounted for.
        report.skipped_marker = True
    return report
