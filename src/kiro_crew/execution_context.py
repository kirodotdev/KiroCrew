"""Immutable memory routing owned by each durable execution record.

These values route built-in operations. They are not a same-host confidentiality
boundary and do not grant transport, app, owner or governance permissions.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import asdict, dataclass, replace
from threading import RLock
from typing import Any, Literal, Mapping, overload

from kiro_crew.validation import MAX_SHORT_STRING

EXECUTION_CONTEXT_KEY = "execution_context"
MEMORY_MODES = ("persistent", "incognito", "temporary")
# Restricted sessions own their record in memory for their lifetime.
_LIVE_EXECUTIONS: dict[tuple[str, str], ExecutionContext] = {}
# What THIS process committed as a session's identity, for the one question the
# durable record cannot answer with authority: "which store does that OTHER
# session belong to?" The record is metadata on the session's own transcript, so
# the session being asked about is the party that writes it.
#
# Deliberately NOT consulted by `read_session_execution`. Several modules publish
# an execution record by its literal key without going through
# `bind_session_execution` (agent selection, the task runner, workflows,
# subagents, MCP control), so a map that shadowed that reader would serve a stale
# identity for the rest of the session after any of them wrote. Keeping this map
# off that path means it can only ever be consulted by a caller that has decided
# it wants THIS process's word rather than the record's.
#
# Insertion-ordered and BOUNDED, because the population is not the set of live
# sessions: every persistent `bind_session_execution` vouches, and several of its
# callers mint a fresh key per request rather than per session -- a webhook with
# no `sessionKey` gets `hook:default:<unix seconds>`, and a task runner refine run
# gets one per run. Those keys are released by their own turn's teardown where one
# exists, so the cap is the backstop for uptime, not the primary release: an entry
# whose producer has no teardown would otherwise live until the process restarts.
_VOUCHED_EXECUTIONS: "OrderedDict[tuple[str, str], ExecutionContext]" = OrderedDict()
# TWO named bounds, one per dimension this map adds: the COUNT of entries, and the
# length of each retained STRING (`MAX_SHORT_STRING`, the repository's own bound for
# names and ids, rather than a literal of this module's). A count cap alone does not
# bound memory, because 4096 rows of an unbounded field is unbounded.
#
# The retained KEY string needs no bound here and deliberately has none: `_vouch` runs
# strictly after the durable write that commits the identity, and that write names a
# file after the session key -- a key the filesystem cannot name raises before the
# vouch is reached (measured: OSError 36 out of `history.py`), so the map cannot hold a
# key longer than the record it corroborates. A second literal for it would be two
# bounds on one population.
#
# `_LIVE_EXECUTIONS` above is deliberately NOT bounded by these. It is main's map,
# written only on the restricted branch and released by that session's own close,
# and evicting a restricted session's carrier would drop the authority that path
# reads instead of merely withdrawing this process's word. Bounding it is a
# separate change to behaviour this PR does not own.
_MAX_VOUCHED_EXECUTIONS = 4096
_vouched_overflow_reported = False
_vouched_overflow_count = 0
_EXECUTION_LOCK = RLock()


def _oversized_retained_field(execution: ExecutionContext) -> str | None:
    """Name the first retained string over the bound, or None when all fit.

    Only the free-form fields are checked. ``selection_kind`` and ``memory_mode``
    are already bounded by ``__post_init__``, which admits them from a closed set,
    and ``store`` carries no string of its own that this map retains beyond
    ``member_id``, which ``__post_init__`` requires it to equal.

    ``MAX_SHORT_STRING`` rather than a literal of this module's own: it is the
    repository's bound for names, ids and categories, which is what every field
    here is, and one constant for the population is what keeps two stores from
    bounding the same ids differently.
    """
    for name in ("member_id", "template_id", "app", "selection_name", "selection_revision"):
        value = getattr(execution, name)
        if isinstance(value, str) and len(value) > MAX_SHORT_STRING:
            return name
    return None


def _vouch(key: tuple[str, str], execution: ExecutionContext) -> None:
    """Retain *execution* as this process's word on *key*, within the count bound.

    Called with ``_EXECUTION_LOCK`` held. Eviction here fails CLOSED and in
    exactly one direction: the entry is absent, `read_vouched_session_execution`
    answers None, and a caller asking whether that session may reach a private
    store must refuse. Nothing is granted by dropping an entry, and the durable
    record is never touched, so a refused session recovers the moment its owner
    re-selects its agent -- the same deferral a restart already carries.
    """
    oversized = _oversized_retained_field(execution)
    if oversized is not None:
        # A cap on the COUNT bounds memory only if each retained item is bounded
        # too, and these strings are not all config-derived: the provider-switch
        # path builds an execution from the session's OWN record with
        # `dataclass_replace(prior, ...)`, so a session that writes an oversized
        # field into its transcript can reach this line. Dropped rather than
        # truncated, the same policy `MAX_ACP_SESSION_ID_LEN` states for a
        # retained backend-authored id: a truncated identity would compare equal
        # to the honest session that owns the shortened form and vouch for it.
        _note_vouched_overflow(
            "an execution whose %s exceeds %d chars was not vouched",
            oversized,
            MAX_SHORT_STRING,
        )
        # Withdrawn, not left standing: this admission refused to vouch for the
        # execution, so any entry a previous admission left under the same key
        # must go with it rather than keep answering on its behalf.
        _withdraw_vouched(key)
        return
    _VOUCHED_EXECUTIONS[key] = execution
    _VOUCHED_EXECUTIONS.move_to_end(key)
    while len(_VOUCHED_EXECUTIONS) > _MAX_VOUCHED_EXECUTIONS:
        evicted, _ = _VOUCHED_EXECUTIONS.popitem(last=False)
        _note_vouched_overflow(
            "vouched identities hit the %d cap; oldest entry for %r was dropped "
            "and that session must be re-bound before it is vouched again",
            _MAX_VOUCHED_EXECUTIONS,
            evicted[1],
        )


def _rearm_vouched_overflow() -> None:
    """Allow the next overflow episode to be reported. The tally is cumulative."""
    global _vouched_overflow_reported

    _vouched_overflow_reported = False


def _rearm_overflow_if_below_cap() -> None:
    """Re-arm the throttle when the population genuinely dropped BELOW the cap.

    Called from `_withdraw_vouched` and nowhere else: a shrink that leaves the
    throttle armed silences the NEXT episode's line, which is the one thing the
    throttle must not do. The eviction `popitem` in `_vouch` is deliberately not a
    caller, because it shrinks and reports in the same breath, so it ARMS rather
    than owing a re-arm.

    Strictly below, not at it: `_vouch` trims to the cap on every insert and every
    other path only pops, so the length can never exceed the cap and an `<=` here
    would hold unconditionally -- every shrink would re-arm, and under sustained cap
    pressure the throttle would emit a line per evicting bind instead of one per
    episode.
    """
    if _vouched_overflow_reported and len(_VOUCHED_EXECUTIONS) < _MAX_VOUCHED_EXECUTIONS:
        _rearm_vouched_overflow()


def _withdraw_vouched(key: tuple[str, str]) -> None:
    """Withdraw one key from the vouched map, paying the throttle's re-arm with it.

    INVARIANT: this is the only statement outside `_vouch`'s eviction loop that
    shrinks `_VOUCHED_EXECUTIONS`. Holding the pop and the re-arm together leaves
    no bare pop for another withdrawal site to copy, so a shrink that leaves the
    next eviction episode silent cannot be written rather than having to be
    noticed. `test_only_the_withdraw_helper_may_shrink_the_vouched_map` enforces
    that.
    """
    _VOUCHED_EXECUTIONS.pop(key, None)
    _rearm_overflow_if_below_cap()


def _note_vouched_overflow(message: str, *args: Any) -> None:
    """Count every overflow, and say it out loud once per episode.

    Both halves are required, and for different readers. The COUNT is why a
    dropped entry is not silent: `read_vouched_session_execution` answers None for
    an evicted key exactly as it does for one never vouched, so without a tally
    the cap is indistinguishable from a session that was never bound. The log line
    names the key that went, so the two can be told apart at the moment it
    happens.

    Once per episode rather than once per eviction, because at the cap every
    further admission evicts and a line each would drown the one that explains
    them. `clear_session_execution` re-arms it when the map falls back under the
    cap, so a later episode is heard rather than swallowed by the first. The
    number in the line is the ALL-TIME tally, not this episode's: the re-arm
    resets only the reported flag, so a second episode's line continues the
    count rather than restarting it.
    """
    global _vouched_overflow_reported, _vouched_overflow_count

    _vouched_overflow_count += 1
    if _vouched_overflow_reported:
        return
    _vouched_overflow_reported = True
    logging.getLogger(__name__).warning(
        "vouched execution overflow (%d dropped in total): " + message,
        _vouched_overflow_count,
        *args,
    )


def _live_key(session_key: str) -> tuple[str, str]:
    from kiro_crew.config.paths import data_home

    return str(data_home()), session_key


def clear_session_execution(
    session_key: str, *, expected: ExecutionContext | None | object = ...
) -> None:
    with _EXECUTION_LOCK:
        key = _live_key(session_key)
        if expected is ... or _LIVE_EXECUTIONS.get(key) == expected:
            _LIVE_EXECUTIONS.pop(key, None)
        # The vouched entry is withdrawn under its OWN compare-and-set, not the
        # one above. A persistent session never had a live carrier, so keying
        # this withdrawal on `_LIVE_EXECUTIONS` would compare against None, never
        # fire, and leave the entry alive for the rest of the process.
        if expected is ... or _VOUCHED_EXECUTIONS.get(key) == expected:
            # Inside the branch, not after it: only a withdrawal whose
            # compare-and-set actually matched has shrunk anything, and a clear
            # whose key was absent must not re-arm on another episode's behalf.
            _withdraw_vouched(key)


def _unavailable(message: str):
    from kiro_crew.memory_stores import UnknownMemoryStore

    return UnknownMemoryStore(f"Execution memory is unavailable: {message}; Global was not used")


def stricter_memory_mode(*modes: str) -> str:
    if not modes or any(mode not in MEMORY_MODES for mode in modes):
        raise _unavailable("invalid privacy mode")
    return max(modes, key=MEMORY_MODES.index)


@dataclass(frozen=True)
class MemoryStoreRef:
    store_id: str
    member_id: str | None = None

    def __post_init__(self) -> None:
        from kiro_crew.memory_stores import validate_memory_store_name

        validate_memory_store_name(self.store_id)
        if self.member_id is not None:
            from kiro_crew.members import validate_slug

            validate_slug(self.member_id)
            if self.store_id == "default":
                raise _unavailable("a member cannot use the Global store")

    @property
    def legacy_name(self) -> str:
        return "" if self.store_id == "default" else self.store_id


@dataclass(frozen=True)
class ExecutionContext:
    member_id: str | None
    store: MemoryStoreRef
    selection_kind: str
    template_id: str
    memory_mode: str = "persistent"
    app: str = ""
    selection_name: str = ""
    selection_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.store, MemoryStoreRef) or self.store.member_id != self.member_id:
            raise _unavailable("member and store identity disagree")
        if self.selection_kind not in ("member", "template"):
            raise _unavailable("invalid agent namespace")
        if not isinstance(self.template_id, str) or (
            self.member_id is not None and not self.template_id
        ):
            raise _unavailable("missing execution template")
        if (
            not isinstance(self.app, str)
            or not isinstance(self.selection_name, str)
            or not isinstance(self.selection_revision, str)
        ):
            raise _unavailable("invalid execution attribution")
        stricter_memory_mode(self.memory_mode)

    def to_record(self) -> dict[str, Any]:
        return asdict(self)

    def with_mode(self, mode: str) -> ExecutionContext:
        return replace(self, memory_mode=stricter_memory_mode(self.memory_mode, mode))


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: Literal[True] = True
) -> ExecutionContext: ...


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: Literal[False]
) -> ExecutionContext | None: ...


@overload
def execution_from_record(
    record: Mapping[str, Any], *, required: bool
) -> ExecutionContext | None: ...


def execution_from_record(
    record: Mapping[str, Any], *, required: bool = True
) -> ExecutionContext | None:
    """Decode the owner's canonical field; never infer membership from a name."""
    if not isinstance(record, Mapping):
        raise _unavailable("execution record is not an object")
    payload = record.get(EXECUTION_CONTEXT_KEY)
    if payload is None and EXECUTION_CONTEXT_KEY not in record and not required:
        return None
    if not isinstance(payload, dict):
        raise _unavailable("missing or malformed execution context")
    try:
        store = payload["store"]
        if not isinstance(store, dict):
            raise ValueError("invalid store")
        return ExecutionContext(
            member_id=payload["member_id"],
            store=MemoryStoreRef(store_id=store["store_id"], member_id=store["member_id"]),
            selection_kind=payload["selection_kind"],
            template_id=payload["template_id"],
            memory_mode=payload["memory_mode"],
            app=payload["app"],
            selection_name=payload.get("selection_name", ""),
            selection_revision=payload.get("selection_revision", ""),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise _unavailable("malformed execution context") from exc


def member_config_for_id(config: Any, member_id: str) -> tuple[str, Any]:
    """Find the unique persisted ID; names and slug candidates are never identity."""
    matches = [
        (name, member)
        for name, member in config.agents.items()
        if getattr(member, "member_id", "") == member_id
    ]
    if not member_id or len(matches) != 1:
        raise _unavailable("member identity is missing or ambiguous")
    return matches[0]


def resolve_member_execution(
    config: Any,
    member: str,
    *,
    memory_mode: str = "persistent",
    app: str = "",
    validate_memory_files: bool = False,
) -> ExecutionContext:
    """Capture an explicitly selected existing member and its store together."""
    from kiro_crew.memory_stores import require_member_memory_store

    alias, agent = (
        (member, config.agents[member])
        if member in config.agents
        else member_config_for_id(config, member)
    )
    store = require_member_memory_store(config, alias, require_directory=validate_memory_files)
    declaration = config.memory_stores.get(store)
    member_id = getattr(agent, "member_id", "") or None
    if getattr(declaration, "memory_version", 1) == 2 and not member_id:
        raise _unavailable("member has no persisted identity")
    return ExecutionContext(
        member_id=member_id,
        store=MemoryStoreRef(store, member_id),
        selection_kind="member",
        template_id=agent.kiro_agent or "kirocrew",
        memory_mode=memory_mode,
        app=app,
        selection_name=alias,
    )


def execution_for_store(
    store: str, *, memory_mode: str = "persistent", app: str = "", template_id: str = ""
) -> ExecutionContext:
    """Admission adapter for a store already selected by trusted gateway code."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.memory_stores import require_memory_store

    if not isinstance(store, str):
        raise _unavailable("memory identity is malformed")
    config = KiroCrewConfig.load()
    name = require_memory_store(store or "default", config=config, require_directory=False)
    declaration = config.memory_stores.get(name)
    if getattr(declaration, "memory_version", 1) == 2:
        member_id = getattr(declaration, "owner_member_id", "")
        alias, _ = member_config_for_id(config, member_id)
        resolved = resolve_member_execution(config, alias, memory_mode=memory_mode, app=app)
        if resolved.store.store_id != name:
            raise _unavailable("member store binding changed")
        return resolved
    return ExecutionContext(
        None, MemoryStoreRef(name), "template", template_id, memory_mode, app, template_id
    )


def validate_execution(
    execution: ExecutionContext, *, validate_memory_files: bool = True
) -> ExecutionContext:
    """Check only the captured store, never re-resolve a live member selection."""
    from kiro_crew.memory_stores import MEMORY_DB_FILE, _named_store_dir

    if execution.member_id is not None:
        path = _named_store_dir(execution.store.store_id) / MEMORY_DB_FILE
        if not validate_memory_files:
            return execution
        from kiro_crew.vector_memory import read_member_database_identity, sqlite3

        try:
            identity = read_member_database_identity(path)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise _unavailable("member database is unreadable") from exc
        if identity != (execution.member_id, execution.store.store_id):
            raise _unavailable("stored database identity changed")
    return execution


def derive_execution(
    parent: ExecutionContext,
    *,
    target_member: str | None = None,
    config: Any = None,
    requested_mode: str | None = None,
) -> ExecutionContext:
    """Inherit by default; an explicit target is resolved by the admitted caller."""
    mode = stricter_memory_mode(parent.memory_mode, requested_mode or parent.memory_mode)
    if target_member is None:
        return parent.with_mode(mode)
    if not target_member:
        raise _unavailable("target member must be explicit")
    if config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        config = KiroCrewConfig.load()
    return resolve_member_execution(config, target_member, memory_mode=mode, app=parent.app)


def refresh_vouched_session_execution(session_key: str) -> None:
    """Mark *session_key*'s vouched entry as recently used, if one is still held.

    Eviction at the cap drops the OLDEST entry and `_vouch` orders by last BIND,
    so without this the order is birth order for a session's whole life: a member
    session that binds once and then dispatches all day stays at the head of the
    queue, while producers that mint a fresh key per request -- a webhook with no
    `sessionKey`, a task-runner refine run -- churn newer entries in behind it. At
    the cap that evicts the long-lived dispatcher first, which is backwards: it is
    the one still in use.

    Called on a SUCCESSFUL own-store admission, so recency follows use rather than
    birth. It grants nothing: the entry must already be present, its value is not
    touched, and an absent key is a no-op. The only thing it changes is WHICH
    entry a later overflow drops, and dropping only ever refuses.
    """
    key = _live_key(session_key)
    with _EXECUTION_LOCK:
        if key in _VOUCHED_EXECUTIONS:
            _VOUCHED_EXECUTIONS.move_to_end(key)


def read_vouched_session_execution(session_key: str) -> ExecutionContext | None:
    """This process's own word on *session_key*'s identity, or None.

    None is a real answer and the safe one: it means this process has not
    committed an identity for that session under the home in force, so a caller
    deciding whether the session may reach a private store has nothing to go on
    and must refuse. It is never a licence to fall back to the durable record --
    the record is what the subject session writes, so falling back would hand the
    subject the answer to a question about itself.
    """
    with _EXECUTION_LOCK:
        return _VOUCHED_EXECUTIONS.get(_live_key(session_key))


def read_live_session_execution(session_key: str) -> ExecutionContext | None:
    """Snapshot the live carrier for generation-safe restricted-session cleanup."""
    with _EXECUTION_LOCK:
        return _LIVE_EXECUTIONS.get(_live_key(session_key))


@overload
def read_session_execution(session_key: str, *, required: Literal[True]) -> ExecutionContext: ...


@overload
def read_session_execution(
    session_key: str, *, required: Literal[False] = False
) -> ExecutionContext | None: ...


@overload
def read_session_execution(session_key: str, *, required: bool) -> ExecutionContext | None: ...


def read_session_execution(session_key: str, *, required: bool = False) -> ExecutionContext | None:
    from kiro_crew.history import ConversationLog

    with _EXECUTION_LOCK:
        live = _LIVE_EXECUTIONS.get(_live_key(session_key))
    if live is not None:
        return live
    if not session_key:
        if required:
            raise _unavailable("missing session")
        return None
    if session_key.startswith("subagent:"):
        from kiro_crew.subagent_persistence import read_run_execution, read_state

        record = read_state(session_key.split(":", 1)[1])
        if record is not None:
            return read_run_execution(session_key.split(":", 1)[1])
    record, readable = ConversationLog().get_metadata_status(session_key)
    if not readable:
        raise _unavailable("session record is unreadable")
    execution = execution_from_record(record, required=required)
    if execution is None:
        from kiro_crew.memory_stores import MissingExecutionIdentity

        missing = MissingExecutionIdentity(
            "Execution memory is unavailable: session has no canonical member identity; "
            "Global was not used"
        )
        if record.get("member_id") or record.get("selection_kind") == "member":
            raise missing
        store = record.get("memory_store")
        if store and store != "default":
            from kiro_crew.memory_stores import memory_store_version

            if memory_store_version(store) == 2:
                raise missing
    return execution


def capture_session_execution(session_key: str, *, template_id: str = "") -> ExecutionContext:
    """Capture an existing carrier, or an explicitly ordinary V1 session."""
    existing = read_session_execution(session_key)
    if existing is not None:
        return existing
    from kiro_crew.history import ConversationLog

    metadata, readable = (
        ConversationLog().get_metadata_status(session_key) if session_key else ({}, True)
    )
    if not readable:
        raise _unavailable("session metadata is unreadable")
    mode = metadata.get("memory_mode", "persistent")
    store = metadata.get("memory_store", "")
    if not isinstance(store, str):
        raise _unavailable("invalid V1 store")
    if store and store != "default":
        return execution_for_store(store, memory_mode=mode, template_id=template_id)
    return ExecutionContext(None, MemoryStoreRef("default"), "template", template_id, mode)


def bind_session_execution(
    session_key: str,
    execution: ExecutionContext,
    *,
    replace_existing: bool = False,
    expected: ExecutionContext | None | object = ...,
    vouch: bool = False,
) -> None:
    """Publish inside the session's own record, preserving concurrent identity.

    ``vouch=True`` additionally claims own-store authority for the published
    identity, and ONLY a caller whose store was established independently of the
    session's own record may pass it. The vouched map is one of the two sources the
    own-store admission requires to agree, so vouching an execution whose store was
    taken from that record collapses both sources into one value the session
    controls -- the forgery the agreement requirement exists to refuse.

    The default is False because the consequences are asymmetric: a caller that
    should have vouched and did not loses a capability loudly, at a refused
    dispatch, while one that vouches a record-derived store grants access to
    another member's private memory silently. ``test_every_binder_declares_whether
    _it_vouches`` enumerates the call sites so a new binder has to answer this
    question rather than inherit an answer.

    Vouching does not withdraw an existing entry: a legitimate template switch
    republishes the store the owner already established, so leaving that entry keeps
    the capability while a forged store still disagrees with it.
    """
    from kiro_crew.history import ConversationLog

    if not session_key:
        raise _unavailable("missing session")
    log = ConversationLog()
    current = read_session_execution(session_key)
    if expected is not ... and current != expected:
        raise _unavailable("session changed during admission")
    if current is not None:
        execution = execution.with_mode(current.memory_mode)
    if current is not None and not replace_existing:
        if replace(current, memory_mode=execution.memory_mode) != execution:
            raise _unavailable("session already belongs to another execution")
    if session_key.startswith("subagent:"):
        from kiro_crew.subagent_persistence import update_execution_context

        update_execution_context(session_key.split(":", 1)[1], execution, expected=current)
        return
    if execution.memory_mode != "persistent":
        metadata, readable = log.get_metadata_status(session_key)
        if not readable:
            raise _unavailable("session record is unreadable")
        durable = execution_from_record(metadata, required=False)
        if durable is not None:
            # Only retained identity/mode metadata is tightened. Never write a
            # new restricted selection or body merely to keep routing alive.
            retained = durable.with_mode(execution.memory_mode)
            if retained != durable and not log.update_metadata_if(
                session_key,
                {EXECUTION_CONTEXT_KEY: retained.to_record(), "memory_mode": retained.memory_mode},
                lambda meta: meta.get(EXECUTION_CONTEXT_KEY) == durable.to_record(),
            ):
                raise _unavailable("session changed during privacy tightening")
        elif metadata:
            retained_mode = stricter_memory_mode(
                metadata.get("memory_mode", "persistent"), execution.memory_mode
            )
            if not log.update_metadata_if(
                session_key,
                {"memory_mode": retained_mode},
                lambda meta: meta == metadata,
            ):
                raise _unavailable("session changed during privacy tightening")
        with _EXECUTION_LOCK:
            latest = _LIVE_EXECUTIONS.get(_live_key(session_key))
            if latest is not None and latest != current:
                raise _unavailable("session changed during admission")
            _LIVE_EXECUTIONS[_live_key(session_key)] = execution
            # A session that has just become restricted stops being vouched for.
            # Nothing downstream would admit it anyway, since a restricted caller
            # is refused before the store question is reached, but leaving a
            # persistent-era entry behind would leave this map disagreeing with
            # the record it exists to corroborate.
            _withdraw_vouched(_live_key(session_key))
        return
    expected = current.to_record() if current is not None else None
    fields = {
        EXECUTION_CONTEXT_KEY: execution.to_record(),
        "memory_store": execution.store.legacy_name,
        "memory_mode": execution.memory_mode,
    }
    if not log.update_metadata_if(
        session_key, fields, lambda meta: meta.get(EXECUTION_CONTEXT_KEY) == expected
    ):
        raise _unavailable("session changed during admission")
    # Vouch for what was just committed, AFTER the compare-and-set above, so this
    # process never vouches for an identity the durable record does not carry.
    #
    # Only when the store was independently established. A caller that rebuilt this
    # execution from the session's own record carries a store the session itself
    # chose, and vouching it would make the admission's two sources -- the durable
    # record and this map -- one value instead of two.
    #
    # No compare-and-set of its own, unlike the restricted branch above, and the
    # asymmetry is deliberate: that branch has no durable CAS to lean on, while
    # this path is already serialised by the one that just succeeded. Two
    # concurrent persistent admissions read the same `current`, so the loser's CAS
    # fails and it raises above without ever reaching this line. The winner
    # therefore owns the vouched entry.
    if vouch and execution.member_id:
        with _EXECUTION_LOCK:
            _vouch(_live_key(session_key), execution)
    # A member-less execution is NOT vouched even when the caller asks. The own-store
    # admission identifies the caller by ``member_id`` and refuses before the store
    # question when there is none, so such an entry could never be admitted -- it
    # would only occupy a slot in a capped map and invite a later reader to treat
    # "vouched" as meaning more than it does. The template branch of
    # ``record_agent_selection`` reaches here with ``member_id=None`` whenever no
    # prior member is recorded, so this is a live shape, not a defensive one.


def restore_live_session_execution(session_key: str, prior, published) -> bool:
    """CAS rollback a restricted admission; False means use the durable owner."""
    with _EXECUTION_LOCK:
        key = _live_key(session_key)
        # The vouched entry rolls back on its OWN terms, before and regardless of
        # what the live carrier says. A persistent session has no live carrier, so
        # the `current is None` return below would otherwise leave this process
        # still vouching for an identity the rollback has just abandoned -- and a
        # session that can rewrite its own record could then move that record back
        # to the abandoned store and re-establish agreement, which is exactly the
        # forgery the agreement requirement exists to refuse.
        #
        # Same compare-and-set shape as the carrier: only withdraw what THIS
        # admission published, so a newer identity is never erased.
        vouched = _VOUCHED_EXECUTIONS.get(key)
        if vouched is not None and vouched.to_record() == published:
            # WITHDRAW, never restore. Re-vouching `prior` would manufacture
            # authority this process cannot verify: `prior` is the record the
            # SESSION writes, the publication above already overwrote whatever
            # entry existed before it, and nothing reachable here tells a prior
            # that WAS legitimately vouched from a forged one that never was. So
            # restoring it would let a publish-then-rollback hand a forged store
            # the vouched half of the agreement -- the same forgery this map
            # exists to refuse, and the one the paragraph above describes.
            #
            # Dropping it defers own-store dispatch until the owner re-selects the
            # agent, which binds afresh through the durable path. That is the
            # fail-closed direction and the recovery the spec already documents.
            _withdraw_vouched(key)
        current = _LIVE_EXECUTIONS.get(key)
        if current is None:
            return False
        if current.to_record() == published:
            if prior is None:
                _LIVE_EXECUTIONS.pop(key, None)
            else:
                previous = execution_from_record({EXECUTION_CONTEXT_KEY: prior})
                _LIVE_EXECUTIONS[key] = previous.with_mode(current.memory_mode)
        return True
