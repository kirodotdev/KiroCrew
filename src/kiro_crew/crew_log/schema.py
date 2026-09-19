"""Wire format for the append-only crew log: the header, the entry, the rules.

One crew log is one file. Line 1 is a **header** describing the unit the file
belongs to; every later line is an **entry**. Nothing is ever rewritten, so the
format has to be readable by a consumer newer *or* older than the writer:

- Unknown envelope keys are ignored rather than rejected, and an unparseable
  interior line is skipped rather than failing the read (see
  :mod:`kiro_crew.crew_log.store`). ``version`` is the escape hatch for a future
  incompatible break, not a per-change workflow.
- An unknown TYPE is a different question from an unknown key, and the writer
  answers it per entry. ``ignorable: true`` says a reader may skip this line
  when it does not know the type; absent, an unknown type stops reconstruction,
  because a required entry a reader cannot interpret may change the meaning of
  everything after it. Skipping keys loses a detail, skipping a line can lose
  the plot.
- The envelope carries everything a fold needs on one axis, because this file is
  the only structured record of the unit's history: ``type`` spelled
  ``domain/action``, ``time`` in epoch milliseconds, a writer-assigned ``seq``
  contiguous within the file, a ``thread`` grouping key, and a ``ref`` pointer
  into another file. A fact with no unit to belong to -- a script cron, gateway
  lifecycle -- is written to a ``gateway``-kind crew log when one is needed.

Two rules decide whether an entry may be written at all, and they are separate
because they answer different questions.

**Ownership** (rule 1) answers *does this kind of unit have such events at
all*: a session has turns and tool calls, a crew has members and patrols. It is
a prefix registry, :data:`TYPE_OWNERSHIP`, so adding an action to an existing
domain needs no change here.

**Namespacing** (rule 2) answers *may this emitter write this*, and it is a rule
about ``src`` rather than about the spelling of the type. Each kind accepts its
own emitters (:data:`KIND_FIXED_SOURCES`, :data:`KIND_SOURCE_PREFIXES`): a
session's log takes ``gateway`` and ``acp``, a crew's log takes ``gateway``,
``dashboard``, ``patrol`` and the two guest forms ``crew:<name>`` and
``app:<name>``. A ``crew:<name>`` guest writes the crew kind's own built-in
domains, and its name in ``src`` is the signature -- a type carries the fact,
never the identity of who wrote it, so one ``crew/report`` type serves every
child. An ``app:<name>`` guest writes only under its own ``app:<name>/`` type
prefix, the single guest TYPE namespace this format keeps, for a fact no
built-in domain covers. That prefix is what makes the registry safe to keep
short: an app needs no registry entry, because its own name is its permission,
so an app type is checked by rule 2 *instead of* rule 1.

Caps refuse; they never truncate. An entry over :data:`MAX_ENTRY_BYTES`, or a
``ref`` spanning more than :data:`MAX_REF_SPAN` lines, is refused whole. A
silently clipped record the caller believes landed intact is a loss the caller
cannot detect.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from kiro_crew.crew_log.errors import (
    CODE_BAD_DATA,
    CODE_BAD_HEADER,
    CODE_BAD_HEADER_FIELD,
    CODE_BAD_KIND,
    CODE_BAD_REF,
    CODE_BAD_SRC,
    CODE_BAD_TYPE,
    CODE_ENTRY_TOO_LARGE,
    CODE_EVENT_TYPE_NOT_OWNED,
    CODE_INVALID_ID,
    CODE_NAMESPACE_VIOLATION,
    CODE_UNSUPPORTED_VERSION,
    CrewLogError,
)

#: Header schema version. Additive changes never bump it.
SCHEMA_VERSION = 1

KIND_CREW = "crew"
KIND_SESSION = "session"
KIND_MEMBER = "member"

#: The units that own a crew log. A kind is also the ``unit`` a ref names.
#:
#: A member's log is a third kind rather than a second mechanism beside this
#: one. It is the same sort of artifact serving the same purpose -- an
#: append-only record a reader trusts -- so it belongs under the same root and
#: inherits the same sandbox fence by construction. A parallel store outside
#: ``crew-log`` would need its own fence entry, and the next log added would
#: miss it the same way: an audit record an agent can rewrite is not an audit
#: record, and that property has to hold by where the file lives rather than by
#: someone remembering to list it.
KINDS: frozenset[str] = frozenset({KIND_CREW, KIND_SESSION, KIND_MEMBER})

#: Serialized-line ceiling. A line past this is refused, not clipped.
MAX_ENTRY_BYTES = 64 * 1024

#: Widest span a single ``ref`` may point at. A pointer is a citation, not a
#: bulk export: an unbounded span would let one entry make its reader
#: materialize a whole history.
MAX_REF_SPAN = 500

#: Which ``type`` domains each kind owns (rule 1). Prefix-based on purpose: a
#: new action under an owned domain needs no change here.
TYPE_OWNERSHIP: dict[str, frozenset[str]] = {
    KIND_CREW: frozenset(
        {"member", "activity", "slot", "patrol", "message", "crew", "item", "memory"}
    ),
    # One member's own history. The four domains are the crew kind's member-facing
    # ones, narrowed to what describes a single member: its roster fields and
    # binding, the records of it participating, the slots it drives and the patrol
    # on its DM slot. The crew kind keeps owning them too, because ownership
    # answers "does this KIND have such events" and a crew has member events in
    # the same sense it has messages -- what differs is the unit the file belongs
    # to, which is the kind's job to say.
    KIND_MEMBER: frozenset({"member", "activity", "slot", "patrol"}),
    KIND_SESSION: frozenset(
        {
            "session",
            "turn",
            "step",
            "tool",
            "approval",
            "model",
            "compaction",
            # A session's own bodies, what was put in front of the model, work
            # another model did for it, and the children it spawned. ``message``
            # is owned by both kinds: ownership answers "does this KIND have such
            # events", and both a crew and a session do.
            "message",
            "request",
            "context",
            "background",
            "subagent",
            # The session's own task list, a fold a reader builds rather than
            # reads.
            "plan",
            "write",
        }
    ),
}

#: Emitter prefixes that DO carry an instance id, spelled ``<prefix>:<name>``.
CREW_SOURCE_PREFIX = "crew:"
APP_SOURCE_PREFIX = "app:"

#: The fixed emitters each kind accepts. The lists differ because the writers
#: do: a session's entries come from the gateway and the ACP runtime, while a
#: crew's come from the dashboard, a patrol and the gateway. One list shared
#: across kinds accepts ``patrol`` inside a single session's turn history, which
#: is an authorization hole rather than a convenience, because ``src`` is what a
#: reader attributes an entry to.
#:
#: Adding a source to a kind is ADDITIVE: no reader validates ``src``, so a list
#: names the emitters that exist rather than a ceiling on the format.
#: ``session:<id>`` appears in neither, because no emitter writes it.
KIND_FIXED_SOURCES: dict[str, frozenset[str]] = {
    KIND_CREW: frozenset({"gateway", "dashboard", "patrol"}),
    KIND_SESSION: frozenset({"gateway", "acp"}),
    # A member's log takes the same three writers a crew's does, for the same
    # reason: these facts are observed BY the gateway, the dashboard and the
    # patrol loop about the member, never written by the member itself.
    KIND_MEMBER: frozenset({"gateway", "dashboard", "patrol"}),
}

#: The guest emitter prefixes each kind accepts. A guest names an instance, so
#: it is spelled ``<prefix>:<name>`` and only a crew's log takes one: a session's
#: log is one session's own turn history, which no other unit writes into.
KIND_SOURCE_PREFIXES: dict[str, tuple[str, ...]] = {
    KIND_CREW: (CREW_SOURCE_PREFIX, APP_SOURCE_PREFIX),
    KIND_SESSION: (),
    # A member's log takes app guests and NOT crew guests. An installed app is
    # the whole point of the contribution protocol, so it must be able to append
    # its own facts about a member. A sibling crew has no business in one
    # member's history, and accepting ``crew:<name>`` here would let one write an
    # entry a reader attributes to this member's own record.
    KIND_MEMBER: (APP_SOURCE_PREFIX,),
}

#: Every fixed emitter name the format knows, for a caller that wants the
#: vocabulary rather than the rule. Derived, so it cannot drift from the per-kind
#: lists above -- which are what :func:`require_src` enforces.
FIXED_SOURCES: frozenset[str] = frozenset().union(*KIND_FIXED_SOURCES.values())

#: The one guest TYPE namespace: an app's own facts. A crew's facts are built-in
#: crew domains written under a ``crew:<name>`` ``src``, so the crew kind needs
#: no type namespace of its own.
GUEST_TYPE_PREFIX = APP_SOURCE_PREFIX

# A name segment: a domain, an action, a crew name, an app name. No separator,
# so it can never widen a type into another namespace or a path into another
# directory.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #


def require_unit_id(unit_id: str, *, field: str = "id") -> str:
    """*unit_id* unchanged, or raise ``invalid_id``.

    A unit id names a DIRECTORY, so a separator or a NUL in it would let the
    crew log escape its own root. The raw id is refused loudly rather than folded
    to something safe: a fold would silently merge two units into one file, and
    two units sharing a crew log is worse than a refused write. Containment is
    re-checked symlink-safely when the path is built (``resolved_within``); this
    is the cheap shape gate in front of it.
    """
    if not isinstance(unit_id, str) or not unit_id:
        raise CrewLogError(
            "crew log id must be a non-empty string", code=CODE_INVALID_ID, field=field
        )
    if "\0" in unit_id or "/" in unit_id or "\\" in unit_id:
        raise CrewLogError(
            f"crew log id must not contain a path separator or NUL: {unit_id!r}",
            code=CODE_INVALID_ID,
            field=field,
        )
    if unit_id in {".", ".."}:
        raise CrewLogError(
            f"crew log id must not be a relative path element: {unit_id!r}",
            code=CODE_INVALID_ID,
            field=field,
        )
    return unit_id


def require_kind(kind: str, *, field: str = "kind") -> str:
    """*kind* unchanged, or raise ``bad_kind``."""
    if kind not in KINDS:
        raise CrewLogError(
            f"unknown crew log kind {kind!r}; expected one of {sorted(KINDS)}",
            code=CODE_BAD_KIND,
            field=field,
        )
    return kind


# --------------------------------------------------------------------------- #
# type / src
# --------------------------------------------------------------------------- #


def split_type(entry_type: str) -> tuple[str, str]:
    """``("domain", "action")`` for *entry_type*, or raise ``bad_type``.

    Partitioned on the FIRST slash, which is what lets a guest domain keep its
    colon: ``app:radar/scan`` splits to ``("app:radar", "scan")``. The action
    may not itself contain a slash, so the namespace is exactly one level deep
    and no action can smuggle a second domain behind it.
    """
    if not isinstance(entry_type, str) or not entry_type:
        raise CrewLogError(
            "entry type must be a non-empty string", code=CODE_BAD_TYPE, field="type"
        )
    domain, sep, action = entry_type.partition("/")
    if sep != "/" or not domain or not action:
        raise CrewLogError(
            f"entry type must be spelled domain/action: {entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    if not _SEGMENT_RE.match(action):
        raise CrewLogError(
            f"entry type action is not a plain name: {entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    tail = domain[len(GUEST_TYPE_PREFIX) :] if is_guest_type_domain(domain) else domain
    if not _SEGMENT_RE.match(tail):
        raise CrewLogError(
            f"entry type domain must be a plain name or {GUEST_TYPE_PREFIX}<name>: "
            f"{entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    return domain, action


def is_guest_type_domain(domain: str) -> bool:
    """Whether *domain* is the one guest type namespace, ``app:<name>``."""
    return domain.startswith(GUEST_TYPE_PREFIX)


def guest_source_prefix_of(src: str, kind: str) -> str | None:
    """The guest prefix *src* carries for *kind*, or ``None``.

    Keyed by kind, so the answer is the one this log accepts rather than the
    union: ``crew:qa`` is a guest of a crew's log and nothing at all to a
    session's log.
    """
    for prefix in KIND_SOURCE_PREFIXES.get(kind, ()):
        if src.startswith(prefix):
            return prefix
    return None


def require_src(src: str, *, kind: str) -> str:
    """*src* unchanged, or raise ``bad_src``.

    Checked against what this KIND of crew log accepts. ``kind`` is keyword-only
    and required: it selects the rule, so a caller that omits it must fail
    loudly rather than have its ``src`` measured against some default kind's
    list.
    """
    require_kind(kind)
    if not isinstance(src, str) or not src:
        raise CrewLogError("src must be a non-empty string", code=CODE_BAD_SRC, field="src")
    if src in KIND_FIXED_SOURCES[kind]:
        return src
    guest = guest_source_prefix_of(src, kind)
    if guest is not None and _SEGMENT_RE.match(src[len(guest) :]):
        return src
    accepted = sorted(KIND_FIXED_SOURCES[kind]) + [
        f"{prefix}<name>" for prefix in KIND_SOURCE_PREFIXES[kind]
    ]
    raise CrewLogError(
        f"a {kind} crew log accepts src {accepted}: {src!r}",
        code=CODE_BAD_SRC,
        field="src",
    )


def check_ownership(kind: str, entry_type: str, src: str) -> None:
    """Enforce rules 1 and 2 for one (*kind*, *entry_type*, *src*) triple.

    ``src`` is where authorization lives, so it is settled first: an emitter a
    kind does not accept is refused whatever it writes, and everything below can
    then reason about an emitter this crew log already takes. A guest TYPE
    (``app:<name>/<action>``) is judged by its namespace alone and never against
    the ownership registry, which is why the registry needs no app entries.

    Whether a kind takes guest types at all is read off
    :data:`KIND_SOURCE_PREFIXES` rather than compared against one named kind: a
    kind accepts a guest's TYPE exactly when it accepts that guest's ``src``, so
    the two answers cannot drift apart. A session's log still refuses them,
    because it declares no guest prefix -- one session's own turn history is
    written by nobody else.
    """
    require_kind(kind)
    domain, _action = split_type(entry_type)
    require_src(src, kind=kind)

    if is_guest_type_domain(domain):
        if GUEST_TYPE_PREFIX not in KIND_SOURCE_PREFIXES[kind]:
            raise CrewLogError(
                f"a {kind} crew log does not accept the guest type {entry_type!r}; "
                f"the {GUEST_TYPE_PREFIX} namespace needs a kind that takes "
                f"{GUEST_TYPE_PREFIX} emitters",
                code=CODE_NAMESPACE_VIOLATION,
                field="type",
            )
        if domain != src:
            raise CrewLogError(
                f"guest type {entry_type!r} must be written by src {domain!r}, not {src!r}",
                code=CODE_NAMESPACE_VIOLATION,
                field="type",
            )
        return

    if src.startswith(APP_SOURCE_PREFIX):
        raise CrewLogError(
            f"app emitter {src!r} may only write types under {src}/, not {entry_type!r}",
            code=CODE_NAMESPACE_VIOLATION,
            field="src",
        )
    if domain not in TYPE_OWNERSHIP[kind]:
        raise CrewLogError(
            f"a {kind} crew log does not own the type {entry_type!r}; "
            f"owned domains are {sorted(TYPE_OWNERSHIP[kind])}",
            code=CODE_EVENT_TYPE_NOT_OWNED,
            field="type",
        )


# --------------------------------------------------------------------------- #
# ref
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Ref:
    """A pointer to a segment of another (or the same) crew log.

    A ref is a CITATION, not a copy: the bytes stay in the crew log they were
    written to, and a reader that may not read that unit gets ``forbidden``
    rather than the contents. ``to_seq`` absent means the single line at
    ``from_seq``.
    """

    unit: str
    id: str
    from_seq: int
    to_seq: int | None = None

    def __post_init__(self) -> None:
        if self.unit not in KINDS:
            raise CrewLogError(
                f"ref unit must be one of {sorted(KINDS)}: {self.unit!r}",
                code=CODE_BAD_REF,
                field="unit",
            )
        try:
            require_unit_id(self.id, field="ref.id")
        except CrewLogError as exc:
            raise CrewLogError(exc.message, code=CODE_BAD_REF, field="ref.id") from exc
        if not _is_seq(self.from_seq):
            raise CrewLogError(
                f"ref from must be a positive int: {self.from_seq!r}",
                code=CODE_BAD_REF,
                field="ref.from",
            )
        if self.to_seq is None:
            return
        if not _is_seq(self.to_seq) or self.to_seq < self.from_seq:
            raise CrewLogError(
                f"ref to must be a positive int at or after from: {self.to_seq!r}",
                code=CODE_BAD_REF,
                field="ref.to",
            )
        if self.to_seq - self.from_seq + 1 > MAX_REF_SPAN:
            raise CrewLogError(
                f"ref spans more than {MAX_REF_SPAN} lines; cite a narrower segment",
                code=CODE_BAD_REF,
                field="ref.to",
            )

    @property
    def last_seq(self) -> int:
        """The last seq the ref covers -- ``from_seq`` when ``to_seq`` is absent."""
        return self.from_seq if self.to_seq is None else self.to_seq

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"unit": self.unit, "id": self.id, "from": self.from_seq}
        if self.to_seq is not None:
            out["to"] = self.to_seq
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Ref:
        """A :class:`Ref` from its wire form, or raise ``bad_ref``."""
        if not isinstance(raw, dict):
            raise CrewLogError(f"ref must be an object: {raw!r}", code=CODE_BAD_REF, field="ref")
        # Deliberately ``Any``: these come off the wire untyped, and
        # ``__post_init__`` is the thing that rejects a wrong one -- the same
        # check a direct constructor call goes through, so the two paths cannot
        # disagree about what a valid ref is.
        unit: Any = raw.get("unit")
        unit_id: Any = raw.get("id")
        from_seq: Any = raw.get("from")
        to_seq: Any = raw.get("to")
        return cls(unit=unit, id=unit_id, from_seq=from_seq, to_seq=to_seq)


def _is_seq(value: Any) -> bool:
    """Whether *value* is a usable seq: a positive int, and not a JSON ``true``."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


# --------------------------------------------------------------------------- #
# Entries
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Entry:
    """One crew log line after the header. ``seq`` and ``time`` are writer-assigned.

    ``ignorable`` marks an entry a reader may SKIP when it does not know the
    type. It is the writer's promise that nothing later in the file depends on
    this entry having been interpreted -- a sampled stream body, a hint -- and it
    is the only thing that lets an older reader keep folding a file a newer
    writer extended. Without it an unknown type stops reconstruction
    (:meth:`~kiro_crew.crew_log.store.CrewLog.iter_from` with ``known=``), because a
    required entry a reader cannot interpret may change the meaning of every
    entry after it.
    """

    type: str
    seq: int
    time: int
    src: str
    data: dict[str, Any]
    thread: int | None = None
    ref: Ref | None = None
    ignorable: bool = False

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "seq": self.seq,
            "time": self.time,
            "src": self.src,
        }
        if self.thread is not None:
            out["thread"] = self.thread
        if self.ref is not None:
            out["ref"] = self.ref.to_dict()
        # Written only when set, so every line this format already produced is
        # byte-identical to what it was before the marker existed.
        if self.ignorable:
            out["ignorable"] = True
        out["data"] = self.data
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> Entry | None:
        """An :class:`Entry` from a parsed line, or ``None`` when unusable.

        Returns ``None`` instead of raising: this is the READ path, and a reader
        of an append-only file must not be the thing that crashes because one
        interior line is damaged. Unknown envelope keys are ignored, so a line
        from a newer writer still reads.
        """
        if not isinstance(raw, dict):
            return None
        entry_type = raw.get("type")
        # ``Any`` because ``_is_seq`` is what narrows these, and a static
        # annotation here would only duplicate a check the reader must make
        # against untrusted bytes anyway.
        seq: Any = raw.get("seq")
        stamp = raw.get("time")
        src = raw.get("src")
        data = raw.get("data")
        if not isinstance(entry_type, str) or not entry_type:
            return None
        if not _is_seq(seq) or not isinstance(stamp, int) or isinstance(stamp, bool):
            return None
        if not isinstance(src, str) or not src or not isinstance(data, dict):
            return None
        thread = raw.get("thread")
        if thread is not None and not _is_seq(thread):
            return None
        raw_ref = raw.get("ref")
        ref: Ref | None = None
        if raw_ref is not None:
            try:
                ref = Ref.from_dict(raw_ref)
            except CrewLogError:
                return None
        # Only a literal ``true`` sets it. This marker RELAXES the reader's
        # unknown-type guard, so a truthy coercion would let a damaged line -- or
        # a line written into an agent-writable tree -- switch the guard off with
        # a string or a number. A wrong value reads as absent, which fails closed.
        return cls(
            type=entry_type,
            seq=seq,
            time=stamp,
            src=src,
            data=data,
            thread=thread,
            ref=ref,
            ignorable=raw.get("ignorable") is True,
        )


def serialize(payload: dict[str, Any]) -> str:
    """*payload* as one compact JSON line, or raise ``bad_data``.

    ``ensure_ascii`` stays on so the file is byte-stable regardless of the
    reader's locale, and the separators drop the whitespace ``json`` adds by
    default -- a crew log line is machine-read, and the bytes are the budget the
    size cap is spent from.
    """
    try:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    except (TypeError, ValueError) as exc:
        raise CrewLogError(
            f"crew log payload is not JSON-serializable: {exc}", code=CODE_BAD_DATA, field="data"
        ) from exc


def require_entry_line(line: str) -> str:
    """*line* unchanged, or raise ``entry_too_large``."""
    size = len(line.encode("utf-8"))
    if size > MAX_ENTRY_BYTES:
        raise CrewLogError(
            f"entry is {size} bytes, over the {MAX_ENTRY_BYTES}-byte ceiling",
            code=CODE_ENTRY_TOO_LARGE,
            field="data",
        )
    return line


def require_data(data: Any) -> dict[str, Any]:
    """*data* unchanged, or raise ``bad_data``."""
    if not isinstance(data, dict):
        raise CrewLogError(
            f"entry data must be a JSON object, got {type(data).__name__}",
            code=CODE_BAD_DATA,
            field="data",
        )
    return data


# --------------------------------------------------------------------------- #
# Headers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SessionThread:
    """Where a session hangs off its owning crew's log."""

    crew: str
    seq: int

    def to_dict(self) -> dict[str, Any]:
        return {"crew": self.crew, "seq": self.seq}

    @classmethod
    def coerce(cls, raw: Any) -> SessionThread | None:
        """A :class:`SessionThread` from itself, a dict, or ``None``."""
        if raw is None or isinstance(raw, cls):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("crew"), str) and _is_seq(raw.get("seq")):
            return cls(crew=raw["crew"], seq=raw["seq"])
        raise CrewLogError(
            f"session thread must be null or {{crew, seq}}: {raw!r}",
            code=CODE_BAD_HEADER_FIELD,
            field="thread",
        )


@dataclass(frozen=True)
class CrewHeader:
    """Line 1 of a crew's log: which unit this file belongs to, and since when.

    Deliberately minimal. A crew's display name and template belong to the
    members store, which owns them and can change them; duplicating them here
    would make an append-only file the system of record for values it has no way
    to update, so the first rename would leave a permanent lie on line 1.
    """

    id: str
    created_at: int
    version: int = SCHEMA_VERSION

    kind: str = KIND_CREW

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": KIND_CREW,
            "version": self.version,
            "id": self.id,
            "createdAt": self.created_at,
        }


@dataclass(frozen=True)
class MemberHeader:
    """Line 1 of a member's log: which member this file belongs to, and since when.

    The agent, workspace, memory store and model are NOT here. Those live in the
    agents config, which owns them and can change them, and the roster fields a
    reader folds arrive as ``member/config`` entries precisely so a change is a
    new fact rather than a rewrite of line 1.

    ``name`` is the one exception, and it is optional for the reason the session
    kind's ``task`` and ``cwd`` are optional: a reader that opens this file cold
    needs a display name before it has folded anything, and the slug is a lossy
    fold of the name (``members.slug_for_name``) so it cannot be recovered from
    ``id``. A later rename is still appended as a ``member/config`` fact, which a
    fold applies over this value -- so line 1 is the cold-start answer, not the
    authority.

    A separate class rather than :class:`CrewHeader` reused, because that one
    writes ``"type": "crew"``, and a member's log claiming to be a crew's would be
    exactly the permanent lie on line 1 that its own docstring warns against.
    """

    id: str
    created_at: int
    version: int = SCHEMA_VERSION
    name: str | None = None

    kind: str = KIND_MEMBER

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": KIND_MEMBER,
            "version": self.version,
            "id": self.id,
            "createdAt": self.created_at,
        }
        # Omitted rather than written null when absent: an absent optional field
        # and one explicitly set to null must not read differently to a consumer.
        if self.name is not None:
            out["name"] = self.name
        return out


@dataclass(frozen=True)
class SessionHeader:
    """Line 1 of a session's log."""

    id: str
    owner: str
    agent: str
    created_at: int
    task: str | None = None
    pack: str | None = None
    slot: str | None = None
    thread: SessionThread | None = None
    cwd: str | None = None
    remote: dict[str, Any] | None = None
    version: int = SCHEMA_VERSION

    kind: str = KIND_SESSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": KIND_SESSION,
            "version": self.version,
            "id": self.id,
            "owner": self.owner,
            "task": self.task,
            "pack": self.pack,
            "agent": self.agent,
            "slot": self.slot,
            "thread": None if self.thread is None else self.thread.to_dict(),
            "cwd": self.cwd,
            "remote": self.remote,
            "createdAt": self.created_at,
        }


Header = CrewHeader | MemberHeader | SessionHeader

_REQUIRED_HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CREW: (),
    KIND_SESSION: ("owner", "agent"),
    # None: the slug in ``id`` is the whole identity, and every roster field a
    # reader wants arrives as a ``member/config`` entry it can fold.
    KIND_MEMBER: (),
}

_OPTIONAL_HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CREW: (),
    KIND_SESSION: ("task", "pack", "slot", "thread", "cwd", "remote"),
    KIND_MEMBER: ("name",),
}

_STR_HEADER_FIELDS = frozenset({"owner", "agent", "task", "pack", "slot", "cwd", "name"})


def build_header(kind: str, unit_id: str, created_at: int, fields: dict[str, Any]) -> Header:
    """The header for a new crew log, or raise ``bad_header_field``.

    Unknown keys are refused rather than dropped. On the READ path an unknown
    key is ignored (an older reader must tolerate a newer writer), but on the
    WRITE path a caller that misspells a field would otherwise be told the
    header was stored as asked while the value silently vanished.
    """
    require_kind(kind)
    require_unit_id(unit_id)
    allowed = set(_REQUIRED_HEADER_FIELDS[kind]) | set(_OPTIONAL_HEADER_FIELDS[kind])
    unknown = sorted(set(fields) - allowed)
    if unknown:
        raise CrewLogError(
            f"unknown header field(s) for a {kind} crew log: {unknown}; allowed: {sorted(allowed)}",
            code=CODE_BAD_HEADER_FIELD,
            field=unknown[0],
        )
    missing = [name for name in _REQUIRED_HEADER_FIELDS[kind] if fields.get(name) is None]
    if missing:
        raise CrewLogError(
            f"missing required header field(s) for a {kind} crew log: {missing}",
            code=CODE_BAD_HEADER_FIELD,
            field=missing[0],
        )
    for name in sorted(set(fields) & _STR_HEADER_FIELDS):
        value = fields[name]
        if value is not None and not isinstance(value, str):
            raise CrewLogError(
                f"header field {name!r} must be a string or null, got {type(value).__name__}",
                code=CODE_BAD_HEADER_FIELD,
                field=name,
            )
    if kind == KIND_CREW:
        return CrewHeader(id=unit_id, created_at=created_at)
    if kind == KIND_MEMBER:
        # A distinct variable: the loops above bind ``name`` to a FIELD NAME, and
        # reusing it for a field's value makes one identifier mean two things in
        # one function -- which is also what the type checker objects to.
        display_name = fields.get("name")
        return MemberHeader(
            id=unit_id,
            created_at=created_at,
            name=display_name if isinstance(display_name, str) else None,
        )
    remote = fields.get("remote")
    if remote is not None and not isinstance(remote, dict):
        raise CrewLogError(
            f"header field 'remote' must be an object or null, got {type(remote).__name__}",
            code=CODE_BAD_HEADER_FIELD,
            field="remote",
        )
    return SessionHeader(
        id=unit_id,
        owner=fields["owner"],
        agent=fields["agent"],
        created_at=created_at,
        task=fields.get("task"),
        pack=fields.get("pack"),
        slot=fields.get("slot"),
        thread=SessionThread.coerce(fields.get("thread")),
        cwd=fields.get("cwd"),
        remote=remote,
    )


def parse_header(raw: Any, *, kind: str, unit_id: str) -> Header:
    """A header from its wire form, or raise ``bad_header``.

    The stored ``type`` and ``id`` must match what the caller asked to open. A
    crew log whose header names a different unit is not this unit's crew log, and
    reading it as if it were would attribute one unit's history to another.
    """
    if not isinstance(raw, dict):
        raise CrewLogError("crew log header is not an object", code=CODE_BAD_HEADER, field="type")
    stored_kind = raw.get("type")
    if stored_kind != kind:
        raise CrewLogError(
            f"crew log header says type {stored_kind!r}, opened as {kind!r}",
            code=CODE_BAD_HEADER,
            field="type",
        )
    if raw.get("id") != unit_id:
        raise CrewLogError(
            f"crew log header says id {raw.get('id')!r}, opened as {unit_id!r}",
            code=CODE_BAD_HEADER,
            field="id",
        )
    created_at = raw.get("createdAt")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        raise CrewLogError(
            f"crew log header createdAt must be epoch ms: {created_at!r}",
            code=CODE_BAD_HEADER,
            field="createdAt",
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise CrewLogError(
            f"crew log header version must be an int: {version!r}",
            code=CODE_BAD_HEADER,
            field="version",
        )
    if version > SCHEMA_VERSION:
        # A NEWER file, not a broken one. Refused here -- before the remaining
        # structural checks and before any entry is decoded -- because a future
        # format need not satisfy this build's checks at all: its new required
        # type would surface as `unknown_entry_type` and its header shape as
        # `bad_header`, both of which tell the reader the log is damaged when the
        # truth is that this process is old. The distinction is the whole point of
        # carrying a version, and nothing else in this module reads it.
        raise CrewLogError(
            f"{kind} crew log {unit_id!r} was written by a newer build "
            f"(format version {version}, this build reads {SCHEMA_VERSION}); "
            "upgrade to read it. The file is not damaged.",
            code=CODE_UNSUPPORTED_VERSION,
            field="version",
        )
    if kind == KIND_CREW:
        return CrewHeader(id=unit_id, created_at=created_at, version=version)
    if kind == KIND_MEMBER:
        raw_name = raw.get("name")
        # A non-string ``name`` is dropped rather than refused: an unknown or
        # malformed OPTIONAL field must not make an otherwise readable log
        # unopenable, which is the same rule the envelope applies to unknown keys.
        return MemberHeader(
            id=unit_id,
            created_at=created_at,
            version=version,
            name=raw_name if isinstance(raw_name, str) else None,
        )
    agent = raw.get("agent")
    owner = raw.get("owner")
    if not isinstance(agent, str) or not isinstance(owner, str):
        raise CrewLogError(
            f"session log header needs string owner and agent: {owner!r}, {agent!r}",
            code=CODE_BAD_HEADER,
            field="agent",
        )
    thread_raw = raw.get("thread")
    try:
        thread = SessionThread.coerce(thread_raw)
    except CrewLogError:
        thread = None
    remote = raw.get("remote")
    return SessionHeader(
        id=unit_id,
        owner=owner,
        agent=agent,
        created_at=created_at,
        task=_opt_str(raw.get("task")),
        pack=_opt_str(raw.get("pack")),
        slot=_opt_str(raw.get("slot")),
        thread=thread,
        cwd=_opt_str(raw.get("cwd")),
        remote=remote if isinstance(remote, dict) else None,
        version=version,
    )


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
