"""One-time startup migration: prune the crewmates an older agent sync generated.

An enrol-on-mount build of the dashboard called ``POST /api/agents/sync`` on
every chat mount, and that sync enrolled EVERY user-authored spec under
``~/.kiro/agents`` as a crewmate --
a ``config.agents`` row with no ``member_id``, on the shared ``default`` memory
store, bound to the spec by name. An existing install therefore carries one
crewmate per custom agent, most of them never opened. This module runs once at
gateway startup and:

* **removes** each such crewmate that was never chatted with as a crewmate --
  no DM thread on the Crewmates page, no session that recorded it as its
  agent, and no entry in its member activity log -- by deleting its
  ``config.agents`` row (:func:`remove_never_chatted`);
* **leaves the chatted ones exactly as they are**: on the shared ``default``
  store, with no ``member_id``. A memory binding is identity and is chosen only
  at creation; an existing member keeps its exact V1 binding (see
  ``memory-skills-hooks.md``, "Member memory experience and lifecycle"), and no
  startup pass rewrites it.

Design:

* **Precise identification.** A row is a candidate only when it is EXACTLY
  what the sync wrote: its name is its ``kiro_agent``, that spec is on disk,
  user-authored (``source == "builtin"``), not the runtime's own and not a
  crew's private copy, its ``description`` is the spec's current description
  (the sync copied it from there; a description the owner rewrote is a
  customization), and every other field sits at its default -- no
  ``member_id``, the shared ``default`` store, no model, effort, triggers,
  colour, star, avatar or workspace, and no key the record does not declare
  (:func:`_is_fresh_sync_shape`, tested on the RAW row as ``config.json`` holds
  it, a missing key reading as its default). Both config
  layers are consulted: a name that ``config.local.json`` touches in its own
  ``agents`` section -- ``kirocrew config set --local agents.<name>.model``,
  the capability writer's overlay binding -- is the owner's and is never a
  candidate, because deleting the base row would leave the overlay leaf as a
  crewmate bound to nothing. A row the owner touched in any of those ways is
  the owner's; a hand-made crewmate has a ``member_id``; a package's spec has
  another source; a row whose spec is gone is left alone.
* **Removal needs complete history; the pass always finishes.** Removal is
  decided from three kinds of evidence, each read STRICTLY by this module --
  never through the roster's total-by-contract readers, which answer "absent"
  for a damaged directory or file: every session file's metadata line
  (:func:`_agents_named_in_history`, the union of every agent the record
  names in ``agent`` and in ``execution_context``; message rows carry no
  agent provenance and an agent switch writes none, so there is nothing below
  that line to read), the crewmate's member activity log --
  the per-session pointer ``record_activity`` appends when a chat runs as that
  member, which survives a later agent switch on the same slot
  (:func:`_activity_names_member`) -- and the crewmate's DM binding file
  (:func:`_chatted`). Two kinds of session file are kept apart on purpose
  (:func:`_session_agents_named`): one that READS AND PARSES but names
  nobody or names another agent -- an older build's first line that is not a
  metadata record, a metadata record with no ``agent``, a session that ran as
  some other agent -- is evidence, speaks only for whom it names, and never
  voids the pass; one that CANNOT BE READ -- an open, stat or read failure, a
  first line that is not UTF-8, an empty file, a first line over the budget,
  a first line that is not JSON -- means the history is incomplete, and an
  incomplete history removes nothing: every candidate is kept and listed under
  ``doubted``, the skipped files under ``unreadable_sessions``. The same holds
  per candidate for the other two sources: a member activity log that exists
  but cannot be loaded, a member log whose segments are present but hold
  nothing readable (a torn log the store reads as absent), a legacy activity
  file that cannot be read or parsed or is over budget, a DM binding that
  cannot be resolved, read or parsed -- each keeps that candidate. No
  conversation log at all keeps every candidate. Only a candidate that a
  complete history nowhere names is removed. The pass always completes and
  writes the marker; it never loops boot after boot on one bad file, and a
  row it keeps loses nothing by staying.
* **Agent-writable paths are opened defensively.** The session files and the
  legacy activity files are opened with ``open_file_no_reparse`` (``O_NOFOLLOW``
  / reparse-point refusal settled in the same operation as the open,
  ``O_NONBLOCK`` so a FIFO cannot hang the pass) and read only when ``fstat``
  says regular file -- a link, a FIFO or anything else is not a session file
  and is "no record", neither evidence nor doubt. The legacy activity file
  is streamed under ``MAX_LEGACY_ACTIVITY_BYTES``, the same budget the event
  log's own fold applies; over budget is doubt for that candidate.
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
  count; the slot restores need not wait either, because a row this pass
  removes has no DM binding and no session naming it (either would have kept
  it), so no restore can rebuild a slot for it. That
  function's middleware holds every mutating request on it -- the chat send,
  slot create, slot agent switch, member thread, channel and import routes
  under ``/api/`` and the OpenAI-compatible ``POST /v1/chat/completions`` are
  all such requests -- so no session can bind an agent, and no DM binding can
  appear, between the history snapshot and a candidate's delete. It also holds
  every request under ``/api/members`` whatever its method: the roster read
  folds a member's legacy activity file into the event log and retires it,
  and appends config reconciles to that log -- the very places
  :func:`_activity_names_member` reads -- so a roster load beside the pass
  could move a crewmate's only record out from under it. The writers that do
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
  the base config lock: the base row must still carry the same ``kiro_agent``
  and the fresh-sync shape; the bound SPEC is re-read from disk under
  ``agents_spec_lock`` (nested inside the config lock, the order every other
  spec writer keeps) and its description must still be the one the row was
  judged against -- a spec whose description moved on between the discovery
  snapshot and the delete, by a hand edit of the file while the pass ran, is
  newer evidence, so the row is kept; and the overlay must still not name it,
  read under its own sidecar lock, taken inside the base lock and held until
  the base write has committed, so no overlay leaf can land for the name
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
import stat
import time
from collections.abc import Callable
from pathlib import Path

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
from kiro_crew.platform_compat import file_lock, open_file_no_reparse, open_lock_file

logger = logging.getLogger(__name__)

#: Written under the config directory once a pass completes. Its body is the
#: record of what the pass did, so an operator can see which crewmates left
#: and which were kept because their history could not be read.
PRUNE_MARKER = "crewmate_prune_migrated.json"

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

#: The discovery ``source`` of a user-authored spec under ``~/.kiro/agents``
#: (``kirocrew`` = the runtime's own, ``package`` = installed by a package).
#: Tested on the SPEC the row is bound to, never on the row's own stamp.
USER_SPEC_SOURCE = "builtin"


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
    #: Session files that could not be read or parsed. Any entry here means the
    #: history is incomplete, and every candidate was kept on that ground.
    unreadable_sessions: list[str] = dataclasses.field(default_factory=list)
    refused: list[str] = dataclasses.field(default_factory=list)
    skipped_marker: bool = False
    #: How many :data:`PRUNE_LOCK_WAIT_S` waits passed with another process
    #: holding :data:`PRUNE_LOCK` before this pass got it (or found the marker).
    lock_waits: int = 0


@dataclasses.dataclass(frozen=True)
class SyncedCandidate:
    """What the discovery snapshot recorded about one candidate's spec.

    ``description`` is the spec's description as discovery read it -- the value
    the row was judged against; ``filename`` is the spec file under the agents
    directory, so the delete can re-read that same file under the spec lock and
    confirm the description has not moved on.
    """

    description: str
    filename: str


def marker_path() -> Path:
    return config_dir() / PRUNE_MARKER


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


def _fresh_sync_row(kiro_agent: str, description: str, source: str) -> dict:
    """What ``_do_agents_sync`` wrote for one spec: the binding, the spec's
    description and source, every other field at its default."""
    return dataclasses.asdict(
        KiroCrewAgentConfig(kiro_agent=kiro_agent, description=description, source=source)
    )


def _is_fresh_sync_shape(raw: dict, *, kiro_agent: str, description: str) -> bool:
    """Whether a RAW ``config.agents`` row is exactly a sync-written row.

    Compared field by field against :func:`_fresh_sync_row` built from the
    binding and the SPEC's current ``description`` (the sync copied it from the
    spec, so a row whose description differs from the spec's was edited by the
    owner -- or the spec moved on -- and is kept either way); every other
    declared field must equal its default -- a row the owner gave a model,
    triggers, a colour, a star, an avatar or a workspace is the owner's and is
    never a candidate -- and the row may carry no key the record does not
    declare, since an unknown key is something a writer other than the sync put
    there. A declared key the row lacks reads as its default: a row written by
    a build whose record had fewer fields is still the sync's row.
    """
    expected = _fresh_sync_row(kiro_agent, description, USER_SPEC_SOURCE)
    if set(raw) - set(expected):
        return False
    for key, default in expected.items():
        if raw.get(key, default) != default:
            return False
    return True


def _synced_candidates(
    cfg: KiroCrewConfig, raw_agents: dict, overlay_agents: dict
) -> dict[str, SyncedCandidate]:
    """The crewmates an older sync generated, in config order, each with the
    current description of the spec it is bound to and that spec's filename.

    ``raw_agents`` is the ``agents`` section as ``config.json`` holds it (not
    the default-filled dataclasses): the shape test must see the row the file
    holds, and the same test -- against the same spec description -- is re-run
    inside the delete's lock. ``overlay_agents`` is the same section from
    ``config.local.json``; any name it mentions is excluded, whatever it says
    about it.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import list_agents

    specs = {info.name: info for info in list_agents(agents_dir=kiro_agents_dir_path())}
    out: dict[str, SyncedCandidate] = {}
    for name, agent in cfg.agents.items():
        if name in ("default", cfg.default_agent):
            continue
        if name in overlay_agents:
            continue
        raw = raw_agents.get(name)
        if not isinstance(raw, dict) or name != agent.kiro_agent:
            continue
        spec = specs.get(agent.kiro_agent)
        if (
            spec is None
            or spec.source != USER_SPEC_SOURCE
            or spec.kirocrew_owned
            or spec.private_to
        ):
            continue
        if not spec.filename:
            continue
        description = str(spec.description or "")
        if not _is_fresh_sync_shape(raw, kiro_agent=agent.kiro_agent, description=description):
            continue
        out[name] = SyncedCandidate(description=description, filename=spec.filename)
    return out


def _current_spec_description(filename: str) -> str | None:
    """The bound spec's ``description`` as its file holds it NOW, or ``None``.

    Read through :func:`~kiro_crew.agent_discovery.read_agent_spec_strict` --
    the same hardened gate discovery reads every spec through -- and
    normalised by the same ``spec_str`` discovery applies, so the value is the
    one :func:`_synced_candidates` would record for the file today. ``None``
    means the file cannot be read as a spec any more (gone, unreadable, refused
    by the path fence, not a JSON object): the caller treats that as newer
    evidence and refuses the delete. The file is only read, never written.
    """
    from kiro_crew.agent import kiro_agents_dir_path
    from kiro_crew.agent_discovery import read_agent_spec_strict, spec_str

    path = Path(kiro_agents_dir_path()) / filename
    try:
        data = read_agent_spec_strict(path, operation="crewmate_prune", source="dashboard")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return spec_str(data, "description")


#: Longest metadata line the session scan reads. A real metadata record is a few
#: hundred bytes; the cap bounds the per-file cost on an agent-writable tree.
_SESSION_META_LINE_MAX = 64 * 1024


def _read_first_line_of_regular_file(path: Path, budget: int) -> bytes | None:
    """The first line of *path* when it is a plain regular file, else ``None``.

    Opened with ``open_file_no_reparse``: the link/reparse refusal is settled
    in the same operation as the open (no check-then-open window), and
    ``O_NONBLOCK`` makes a FIFO return at once so ``fstat`` can refuse it
    instead of the open waiting for a writer that never comes. Anything that
    is not a regular file is ``None`` -- "no record" -- never a hang. Reads at
    most ``budget + 1`` bytes, so a caller can tell a line that fits from one
    that does not. Returned raw: decoding is the caller's judgement. I/O
    failures propagate to the caller, except the one that IS the link
    refusal: ``open_file_no_reparse`` reports a link or reparse point at the
    path as ``ELOOP`` (on every platform), and that is the "not a regular
    file" answer arriving from the open rather than from ``fstat``.
    """
    try:
        fd = open_file_no_reparse(path, nonblocking=True)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.readline(budget + 1)
    finally:
        if fd >= 0:
            os.close(fd)


def _session_agents_named(path: Path) -> set[str]:
    """Every agent one session file's metadata line names; empty for nobody.

    Two kinds of file are kept apart here, because the pass treats them
    oppositely:

    * A file that READS AND PARSES is evidence, whatever it says. A first line
      that is a metadata record returns the UNION of every agent it names (see
      below). One that is not a metadata record -- an older build's first
      line, or a record naming no agent (a session that ran as the default
      crew) -- returns the empty set: it speaks for nobody and condemns
      nobody, and the other files still decide. A link, a FIFO or any
      non-regular file is not a session file at all and is empty on the same
      footing.
    * A file that CANNOT BE READ raises :class:`HistoryUnreadable`: the open,
      stat or read failed; the first line is not UTF-8; the file is empty (a
      session file is born with its metadata line, so an empty one is a torn
      write); the first line is longer than :data:`_SESSION_META_LINE_MAX`; or
      the first line is not JSON. The pass reads that as an incomplete history
      and keeps every candidate.

    A file that is gone by the time it is opened raises ``FileNotFoundError``
    unchanged; the caller skips it, since a file that has been deleted is not
    history.

    Usage evidence per session is the union of every agent the record names,
    not one field of it. The metadata record carries the agent in up to three
    places: ``agent`` (the slot-owned name), and inside ``execution_context``
    the ``selection_name`` and the ``template_id`` (the kiro agent the
    selection runs on). They agree after a completed switch, but an
    interrupted one can leave them apart (``chat_persistence._restored_agent_name``
    prefers the durable selection for that reason), and a name any of them
    holds is a name this session ran as. Nothing below the metadata line
    carries agent provenance: ``ConversationLog.append`` writes a row as role,
    content, ``ts``, ``tools``, source thread/user and ``meta.mid``, and an
    agent switch (``api_chat_slot_agent``) rewrites the metadata line in place
    and appends no row -- there is no switch marker to find and no earlier
    agent to recover from the rows. So a session that ran as a crewmate,
    switched, and had that crewmate's name overwritten in every field is
    evidence only through the member activity log
    (:func:`_activity_names_member`); this function does not read past the
    first line, because there is nothing there to read.
    """
    try:
        raw = _read_first_line_of_regular_file(path, _SESSION_META_LINE_MAX)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise HistoryUnreadable(f"session file {path.name} could not be read: {exc}") from exc
    if raw is None:
        return set()
    if len(raw) > _SESSION_META_LINE_MAX:
        raise HistoryUnreadable(
            f"session file {path.name}: first line exceeds {_SESSION_META_LINE_MAX} bytes"
        )
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeError as exc:
        raise HistoryUnreadable(f"session file {path.name} is not UTF-8: {exc}") from exc
    if not text:
        raise HistoryUnreadable(f"session file {path.name} is empty: no metadata line")
    try:
        record = json.loads(text)
    except ValueError as exc:
        raise HistoryUnreadable(f"session file {path.name}: first line is not JSON: {exc}") from exc
    names: set[str] = set()
    if not isinstance(record, dict) or record.get("_type") != "metadata":
        return names
    agent = record.get("agent")
    if isinstance(agent, str) and agent:
        names.add(agent)
    execution = record.get("execution_context")
    if isinstance(execution, dict):
        for field in ("selection_name", "template_id"):
            value = execution.get(field)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _agents_named_in_history(conversation_log) -> tuple[set[str], list[str]]:
    """Every agent any session's metadata line names, plus the files not read.

    ``ConversationLog.agent_usage()`` is built on ``list_sessions()``, which
    skips a file it cannot stat and swallows a first line it cannot read or
    parse. Here every ``*.jsonl`` in the history directory is classified by
    :func:`_session_agents_named`: a file that reads and parses adds every
    agent it names (or nothing) to the first element; a file that cannot be
    read is returned by name in the second. The second element non-empty means
    the history is INCOMPLETE, and the caller removes nothing on it. A
    directory that cannot be listed raises :class:`HistoryUnreadable`.

    A file the listing saw and the open did not find is in the second element
    too: whatever it named was destroyed between the two, and the pass has no
    way to know it was not the one session that ran as a candidate. Nothing
    on the startup path deletes a transcript while the pass runs (the channel
    transcript migration defers its deletes until the pass has settled), so
    this is a user or another process deleting history under the pass, and it
    voids the pass rather than the evidence.

    Every agent-naming field of the record is read, but all of them are
    rewritten in place when the slot switches agents and no row records the
    switch, so together they name the LAST agent a session ran as; a session
    that ran as a crewmate and later switched is found through the member
    activity log instead (:func:`_activity_names_member`).
    """
    history_dir = getattr(conversation_log, "_dir", None)
    if not isinstance(history_dir, Path):
        raise HistoryUnreadable("conversation log exposes no history directory")
    named: set[str] = set()
    unreadable: list[str] = []
    try:
        if not history_dir.exists():
            return named, unreadable
        paths = list(history_dir.glob("*.jsonl"))
    except OSError as exc:
        raise HistoryUnreadable(f"could not list session history: {exc}") from exc
    for path in paths:
        try:
            agents = _session_agents_named(path)
        except (FileNotFoundError, HistoryUnreadable):
            unreadable.append(path.name)
            continue
        named |= agents
    return named, unreadable


def _activity_names_member(slug: str, name: str) -> bool:
    """Whether the member activity log under ``slug`` records a session for ``name``.

    ``record_activity`` appends one ``activity/record`` event per session a chat
    ran as this member, carrying the exact member name (slugs collide) and the
    session key; it is written once per session and never rewritten, so it
    survives the slot switching to another agent afterwards. Two places hold
    it: the member event log, and -- on an install that has not yet folded it
    -- the pre-log ``activity.jsonl`` / ``activity.jsonl.1`` in the member
    directory. Both are read here, STRICTLY and read-only: nothing is created
    or folded, an event log that cannot be loaded or a legacy file that cannot
    be read or parsed raises :class:`HistoryUnreadable`. So does an event log
    whose segment files are present but hold nothing the store will read: the
    store answers "absent" for a zero-byte segment, which is what a torn write
    leaves behind, and a log that may have held this crewmate's record is
    incomplete evidence, not none. Only a log and files that do not exist --
    or that are not regular files -- read as "no record".

    The legacy path is agent-writable, so it is opened through
    ``open_file_no_reparse`` (no link following, no FIFO hang) and streamed
    line by line under ``MAX_LEGACY_ACTIVITY_BYTES``, the budget the event
    log's own fold applies to the same file; a file over budget is doubt.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.crew_log.schema import KIND_MEMBER
    from kiro_crew.crew_log.store import segment_paths
    from kiro_crew.eventlog.log import MemberLog
    from kiro_crew.eventlog.service import MAX_LEGACY_ACTIVITY_BYTES
    from kiro_crew.eventlog.types import ACTIVITY_RECORD

    try:
        log = MemberLog(slug)
        if log.exists():
            # Streamed oldest-first without retaining: the store's own read
            # path, which refuses (``LogCorrupt``) rather than guesses.
            for event in log.iter_events():
                if event.get("type") != ACTIVITY_RECORD:
                    continue
                data = event.get("data") or {}
                if data.get("member") == name and data.get("session"):
                    return True
        elif segment_paths(KIND_MEMBER, slug):
            raise HistoryUnreadable(
                f"{name!r}'s activity log is present but holds nothing readable (torn)"
            )
    except HistoryUnreadable:
        raise
    except Exception as exc:  # noqa: BLE001 -- a log that will not load is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s activity log: {exc}") from exc
    try:
        base = members_mod.member_dir(slug) / members_mod.ACTIVITY_FILE_NAME
    except Exception as exc:  # noqa: BLE001
        raise HistoryUnreadable(f"could not resolve {name!r}'s activity file: {exc}") from exc
    for path in (base.with_name(base.name + ".1"), base):
        try:
            fd = open_file_no_reparse(path, nonblocking=True)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HistoryUnreadable(f"could not open {name!r}'s activity file: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                continue
            fh = os.fdopen(fd, "rb")
        except OSError as exc:
            os.close(fd)
            raise HistoryUnreadable(f"could not read {name!r}'s activity file: {exc}") from exc
        budget = MAX_LEGACY_ACTIVITY_BYTES
        try:
            with fh:
                while True:
                    line = fh.readline(budget + 1)
                    if not line:
                        break
                    budget -= len(line)
                    if budget < 0:
                        raise HistoryUnreadable(
                            f"{name!r}'s activity file {path.name} exceeds "
                            f"{MAX_LEGACY_ACTIVITY_BYTES} bytes"
                        )
                    text = line.decode("utf-8").strip()
                    if not text:
                        continue
                    row = json.loads(text)
                    if isinstance(row, dict) and row.get("member") == name and row.get("session"):
                        return True
        except HistoryUnreadable:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise HistoryUnreadable(
                f"could not read {name!r}'s activity file {path.name}: {exc}"
            ) from exc
    return False


def _chatted(cfg: KiroCrewConfig, name: str, named: set[str]) -> bool:
    """Whether any chat history records this crewmate.

    Three sources, any suffices: a session whose metadata names it as the agent
    (``named``, from :func:`_agents_named_in_history`); the crewmate's member
    activity log (:func:`_activity_names_member`); or the crewmate's own DM
    thread on the Crewmates page -- the thread route writes the binding file
    the first time the owner opens the thread, so a binding that names this
    crewmate IS the evidence, whether or not a message was ever sent.

    The binding is read STRICTLY here, not through ``read_dm_binding``: that
    reader is total by contract and answers "not bound" for an unreadable
    directory, an unreadable file and a malformed payload alike, which a
    removal must never mistake for "never opened". Only a binding file that
    does not exist reads as never opened. Every other failure -- the path
    cannot be resolved (a containment refusal included), the file cannot be
    statted or read, the payload does not parse -- raises
    :class:`HistoryUnreadable`, and the caller keeps the crewmate.
    """
    from kiro_crew import members as members_mod
    from kiro_crew.atomic_write import read_bytes_with_retry

    if name in named:
        return True
    try:
        slug = members_mod.member_slug(name, cfg)
    except members_mod.MemberSlugError:
        # No slug means no DM thread and no activity log can exist; the usage
        # read above covers the rest.
        return False
    if _activity_names_member(slug, name):
        return True
    try:
        path = members_mod.dm_binding_path(slug)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable path is "unknown", never "no"
        # ``MemberSlugError`` included: the slug passed ``member_slug`` above, so
        # here it means the containment check refused a path that resolves
        # outside the trust root -- a binding that may exist, not one that does not.
        raise HistoryUnreadable(f"could not resolve {name!r}'s DM binding: {exc}") from exc
    try:
        raw = read_bytes_with_retry(path)
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001 -- present but unreadable is "unknown"
        raise HistoryUnreadable(f"could not read {name!r}'s DM binding: {exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise HistoryUnreadable(f"{name!r}'s DM binding does not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise HistoryUnreadable(f"{name!r}'s DM binding is not a record")
    # A colliding slug's binding belongs to exactly one crew name; one that
    # names another crew is legitimately not this crewmate's thread.
    return data.get("member") == name


def _never_abandoned() -> bool:
    return False


def remove_never_chatted(
    cfg: KiroCrewConfig,
    candidates: dict[str, SyncedCandidate],
    *,
    abandoned: Callable[[], bool] = _never_abandoned,
) -> tuple[list[str], list[str], list[str]]:
    """Delete the ``config.agents`` rows named; returns ``(removed, refused, abandoned)``.

    ``candidates`` maps each name to what :func:`_synced_candidates` recorded
    about its spec: the description the row was judged against and the spec's
    filename. Each delete re-runs the candidate test on the ROWS AND THE SPEC
    AS THE FILES HOLD THEM, inside the base config lock: the base row must
    carry the same ``kiro_agent`` and still be the fresh-sync shape against
    that recorded description (:func:`_is_fresh_sync_shape`);
    ``config.local.json`` must still not name it, read under the overlay's own
    sidecar lock; and the bound spec, re-read from disk under
    ``agents_spec_lock``, must still carry that same description. Both inner
    locks are taken inside the base lock, overlay then spec (the order every
    binding writer keeps), and HELD UNTIL THE BASE WRITE HAS COMMITTED, so
    neither an overlay writer landing a leaf for the name nor a spec writer
    changing the description can slip between the check and the delete. A row
    or spec that changed meanwhile -- the owner edited
    the row, a member-aware write stamped it, the spec's description moved on,
    the spec vanished or stopped reading as a spec, an overlay leaf appeared --
    is newer evidence and is refused, not deleted. The test is on identity and
    shape, never on equality with a default-filled snapshot: a row written by
    a build whose record had fewer keys must still be recognised as the
    sync's. Nothing but the base row moves: the overlay, the spec under
    ``~/.kiro/agents`` (only read) and any transcript stay.

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
        # leaf for the name, or a spec writer moving the description, after the
        # check and before the base row is gone.
        with contextlib.ExitStack() as locks:

            def _mutate(
                doc: dict,
                _name: str = name,
                _bound: str = kiro_agent,
                _desc: str = candidate.description,
                _file: str = candidate.filename,
                _locks: contextlib.ExitStack = locks,
            ) -> dict | None:
                nonlocal deleted, gave_up
                agents = coerce_dict_section(doc, "agents")
                raw = agents.get(_name)
                if not isinstance(raw, dict) or raw.get("kiro_agent") != _bound:
                    return None
                if not _is_fresh_sync_shape(raw, kiro_agent=_bound, description=_desc):
                    return None
                # Base lock first, overlay lock second, spec lock third -- the
                # order every binding writer keeps (``_write_bindings``, the
                # template create). Both inner locks are entered on the stack
                # that outlives this callback, so they are released only after
                # ``update_config_locked`` has renamed the base file into
                # place: neither an overlay leaf for the name nor an edit of
                # the spec can land between the checks below and the delete.
                # The overlay is read directly under its lock and the spec is
                # re-read under its lock; nothing is written to either. The
                # spec's description is the one spec-derived input to the
                # shape test above, so a stale value would pass the row. A
                # lock that cannot be taken, or an overlay that cannot be
                # read, is doubt, and doubt refuses.
                try:
                    _locks.enter_context(_config_write_lock(overlay_path))
                    overlay = read_config_for_update(overlay_path)
                    _locks.enter_context(agents_spec_lock(Path(kiro_agents_dir_path())))
                except (OSError, ConfigReadError):
                    return None
                current = _current_spec_description(_file)
                if current != _desc:
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
        "unreadable_sessions": report.unreadable_sessions,
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


def prune_synced_crewmates(
    conversation_log, *, abandoned: Callable[[], bool] = _never_abandoned
) -> PruneReport:
    """Run the pass once. Thread-side; safe to call on every boot.

    ``conversation_log`` is the gateway's :class:`~kiro_crew.history.ConversationLog`;
    ``None`` means history is unavailable, so every candidate is kept on doubt.
    Never raises :class:`HistoryUnreadable`: unreadable evidence keeps the
    crewmates it could have vouched for, and the pass still finishes.

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
    candidates = _synced_candidates(cfg, raw_agents, overlay_agents)
    if candidates:
        named: set[str] = set()
        try:
            if conversation_log is None:
                raise HistoryUnreadable("no conversation log; removal needs chat history")
            named, report.unreadable_sessions = _agents_named_in_history(conversation_log)
            if report.unreadable_sessions:
                skipped = ", ".join(sorted(report.unreadable_sessions))
                raise HistoryUnreadable(
                    f"session history incomplete: {len(report.unreadable_sessions)} "
                    f"file(s) could not be read ({skipped})"
                )
        except HistoryUnreadable as exc:
            # Incomplete history: nothing can be shown never to have run as a
            # crewmate, so everyone is kept. The marker still records it.
            for name in candidates:
                report.doubted[name] = str(exc)
            candidates = {}
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
                chatted = _chatted(cfg, name, named)
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
    if report.unreadable_sessions:
        logger.warning(
            "crewmate prune: %d session file(s) could not be read, so the history is "
            "incomplete and every candidate was kept: %s",
            len(report.unreadable_sessions),
            ", ".join(sorted(report.unreadable_sessions)),
        )
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
