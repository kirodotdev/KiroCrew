"""Wire format for the append-only ledger: the header, the entry, the rules.

One ledger is one file. Line 1 is a **header** describing the unit the file
belongs to; every later line is an **entry**. Nothing is ever rewritten, so the
format has to be readable by a consumer newer *or* older than the writer:

- Unknown envelope keys are ignored rather than rejected, and an unparseable
  interior line is skipped rather than failing the read (see
  :mod:`kiro_crew.ledger.store`). ``version`` is the escape hatch for a future
  incompatible break, not a per-change workflow.
- An unknown TYPE is a different question from an unknown key, and the writer
  answers it per entry. ``ignorable: true`` says a reader may skip this line
  when it does not know the type; absent, an unknown type stops reconstruction,
  because a required entry a reader cannot interpret may change the meaning of
  everything after it. Skipping keys loses a detail, skipping a line can lose
  the plot.
- The envelope is field-compatible with the lifecycle event envelope in
  ``events/base.py``: this
  module's ``type`` is that one's ``kind``, and this module's ``time`` is its
  ``ts_ms``. Same ``domain/action`` naming, same epoch-millisecond clock, so a
  later projection can fold both streams on one axis. What this format adds is
  a writer-assigned ``seq`` (contiguous per file, which the lifecycle envelope
  deliberately left unspecified), a ``thread`` grouping key, and a ``ref``
  pointer into another file.

Two rules decide whether an entry may be written at all, and they are separate
because they answer different questions.

**Ownership** (rule 1) answers *does this kind of unit have such events at
all*: a session has turns and tool calls, a crew has members and patrols. It is
a prefix registry, :data:`TYPE_OWNERSHIP`, so adding an action to an existing
domain needs no change here.

**Namespacing** (rule 2) answers *may this emitter write this*. A crew or an app
writing into a crew ledger is a GUEST: it may write only under its own
``crew:<name>/`` or ``app:<name>/`` prefix, and only into a crew ledger. The
guest prefix is what makes the registry safe to keep short -- a guest never
needs a registry entry, because its own name is its permission. Guest types are
therefore checked by rule 2 *instead of* rule 1, not in addition to it.

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

from kiro_crew.ledger.errors import (
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
    LedgerError,
)

#: Header schema version. Additive changes never bump it.
SCHEMA_VERSION = 1

KIND_CREW = "crew"
KIND_SESSION = "session"

#: The two units that own a ledger. A kind is also the ``unit`` a ref names.
KINDS: frozenset[str] = frozenset({KIND_CREW, KIND_SESSION})

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
    KIND_SESSION: frozenset(
        {
            "session",
            "turn",
            "step",
            "tool",
            "approval",
            "model",
            "compaction",
            "remote",
            # A session's own bodies, what was put in front of the model, what
            # was loaded lazily on its behalf, work another model did for it,
            # and the children it spawned. ``message`` is owned by both kinds:
            # ownership answers "does this KIND have such events", and both a
            # crew and a session do.
            "message",
            "request",
            "context",
            "skill",
            "background",
            "subagent",
            # The two folds a reader builds rather than reads: what a compaction
            # replaced (`summary/written`, paired with `compaction/applied`) and
            # the session's own task list (`plan/updated`).
            "summary",
            "plan",
        }
    ),
}

#: Emitters that name a whole subsystem and carry no instance id.
FIXED_SOURCES: frozenset[str] = frozenset({"gateway", "acp", "dashboard", "patrol"})

#: Emitter prefixes that DO carry an instance id, spelled ``<prefix>:<name>``.
SESSION_SOURCE_PREFIX = "session:"
CREW_SOURCE_PREFIX = "crew:"
APP_SOURCE_PREFIX = "app:"

#: The two guest prefixes. A guest's own name is its write permission.
GUEST_PREFIXES: tuple[str, ...] = (CREW_SOURCE_PREFIX, APP_SOURCE_PREFIX)

# A name segment: a domain, an action, a crew name, an app name. No separator,
# so it can never widen a type into another namespace or a path into another
# directory.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# A session id as it appears after ``session:``. Colons are allowed because a
# channel session key legitimately carries one (``slack:1712793600.123``), so a
# colon count cannot classify the tail.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


# --------------------------------------------------------------------------- #
# Ids
# --------------------------------------------------------------------------- #


def require_unit_id(unit_id: str, *, field: str = "id") -> str:
    """*unit_id* unchanged, or raise ``invalid_id``.

    A unit id names a DIRECTORY, so a separator or a NUL in it would let the
    ledger escape its own root. The raw id is refused loudly rather than folded
    to something safe: a fold would silently merge two units into one file, and
    two units sharing a ledger is worse than a refused write. Containment is
    re-checked symlink-safely when the path is built (``resolved_within``); this
    is the cheap shape gate in front of it.
    """
    if not isinstance(unit_id, str) or not unit_id:
        raise LedgerError("ledger id must be a non-empty string", code=CODE_INVALID_ID, field=field)
    if "\0" in unit_id or "/" in unit_id or "\\" in unit_id:
        raise LedgerError(
            f"ledger id must not contain a path separator or NUL: {unit_id!r}",
            code=CODE_INVALID_ID,
            field=field,
        )
    if unit_id in {".", ".."}:
        raise LedgerError(
            f"ledger id must not be a relative path element: {unit_id!r}",
            code=CODE_INVALID_ID,
            field=field,
        )
    return unit_id


def require_kind(kind: str, *, field: str = "kind") -> str:
    """*kind* unchanged, or raise ``bad_kind``."""
    if kind not in KINDS:
        raise LedgerError(
            f"unknown ledger kind {kind!r}; expected one of {sorted(KINDS)}",
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
    colon: ``crew:qa/report`` splits to ``("crew:qa", "report")``. The action
    may not itself contain a slash, so the namespace is exactly one level deep
    and no action can smuggle a second domain behind it.
    """
    if not isinstance(entry_type, str) or not entry_type:
        raise LedgerError("entry type must be a non-empty string", code=CODE_BAD_TYPE, field="type")
    domain, sep, action = entry_type.partition("/")
    if sep != "/" or not domain or not action:
        raise LedgerError(
            f"entry type must be spelled domain/action: {entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    if not _SEGMENT_RE.match(action):
        raise LedgerError(
            f"entry type action is not a plain name: {entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    guest = guest_prefix_of(domain)
    tail = domain[len(guest) :] if guest else domain
    if not _SEGMENT_RE.match(tail):
        raise LedgerError(
            f"entry type domain is not a plain name: {entry_type!r}",
            code=CODE_BAD_TYPE,
            field="type",
        )
    return domain, action


def guest_prefix_of(value: str) -> str | None:
    """The guest prefix *value* starts with (``crew:`` / ``app:``), else ``None``."""
    for prefix in GUEST_PREFIXES:
        if value.startswith(prefix):
            return prefix
    return None


def require_src(src: str) -> str:
    """*src* unchanged, or raise ``bad_src``."""
    if not isinstance(src, str) or not src:
        raise LedgerError("src must be a non-empty string", code=CODE_BAD_SRC, field="src")
    if src in FIXED_SOURCES:
        return src
    if src.startswith(SESSION_SOURCE_PREFIX):
        if _SESSION_ID_RE.match(src[len(SESSION_SOURCE_PREFIX) :]):
            return src
    else:
        guest = guest_prefix_of(src)
        if guest and _SEGMENT_RE.match(src[len(guest) :]):
            return src
    raise LedgerError(
        f"src must be one of {sorted(FIXED_SOURCES)} or "
        f"session:<id> / crew:<name> / app:<name>: {src!r}",
        code=CODE_BAD_SRC,
        field="src",
    )


def check_ownership(kind: str, entry_type: str, src: str) -> None:
    """Enforce rules 1 and 2 for one (*kind*, *entry_type*, *src*) triple.

    Order matters. Whether the TYPE is guest-namespaced is decided first,
    because that answer selects which rule governs: a guest type is judged by
    rule 2 alone (its own name is its permission) and never consulted against
    the ownership registry, which is why the registry needs no guest entries.
    """
    domain, _action = split_type(entry_type)
    require_src(src)
    type_guest = guest_prefix_of(domain)
    src_guest = guest_prefix_of(src)

    if type_guest is not None:
        if kind != KIND_CREW:
            raise LedgerError(
                f"a {kind} ledger does not accept the guest type {entry_type!r}; "
                "guest namespaces are crew-ledger only",
                code=CODE_NAMESPACE_VIOLATION,
                field="type",
            )
        if domain != src:
            raise LedgerError(
                f"guest type {entry_type!r} must be written by src {domain!r}, not {src!r}",
                code=CODE_NAMESPACE_VIOLATION,
                field="type",
            )
        return

    if src_guest is not None:
        raise LedgerError(
            f"guest src {src!r} may only write types under {src}/, not {entry_type!r}",
            code=CODE_NAMESPACE_VIOLATION,
            field="src",
        )
    if domain not in TYPE_OWNERSHIP[require_kind(kind)]:
        raise LedgerError(
            f"a {kind} ledger does not own the type {entry_type!r}; "
            f"owned domains are {sorted(TYPE_OWNERSHIP[kind])}",
            code=CODE_EVENT_TYPE_NOT_OWNED,
            field="type",
        )


# --------------------------------------------------------------------------- #
# ref
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Ref:
    """A pointer to a segment of another (or the same) ledger.

    A ref is a CITATION, not a copy: the bytes stay in the ledger they were
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
            raise LedgerError(
                f"ref unit must be one of {sorted(KINDS)}: {self.unit!r}",
                code=CODE_BAD_REF,
                field="unit",
            )
        try:
            require_unit_id(self.id, field="ref.id")
        except LedgerError as exc:
            raise LedgerError(exc.message, code=CODE_BAD_REF, field="ref.id") from exc
        if not _is_seq(self.from_seq):
            raise LedgerError(
                f"ref from must be a positive int: {self.from_seq!r}",
                code=CODE_BAD_REF,
                field="ref.from",
            )
        if self.to_seq is None:
            return
        if not _is_seq(self.to_seq) or self.to_seq < self.from_seq:
            raise LedgerError(
                f"ref to must be a positive int at or after from: {self.to_seq!r}",
                code=CODE_BAD_REF,
                field="ref.to",
            )
        if self.to_seq - self.from_seq + 1 > MAX_REF_SPAN:
            raise LedgerError(
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
            raise LedgerError(f"ref must be an object: {raw!r}", code=CODE_BAD_REF, field="ref")
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
    """One ledger line after the header. ``seq`` and ``time`` are writer-assigned.

    ``ignorable`` marks an entry a reader may SKIP when it does not know the
    type. It is the writer's promise that nothing later in the file depends on
    this entry having been interpreted -- a sampled stream body, a hint -- and it
    is the only thing that lets an older reader keep folding a file a newer
    writer extended. Without it an unknown type stops reconstruction
    (:meth:`~kiro_crew.ledger.store.Ledger.iter_from` with ``known=``), because a
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
            except LedgerError:
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
    default -- a ledger line is machine-read, and the bytes are the budget the
    size cap is spent from.
    """
    try:
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=False)
    except (TypeError, ValueError) as exc:
        raise LedgerError(
            f"ledger payload is not JSON-serializable: {exc}", code=CODE_BAD_DATA, field="data"
        ) from exc


def require_entry_line(line: str) -> str:
    """*line* unchanged, or raise ``entry_too_large``."""
    size = len(line.encode("utf-8"))
    if size > MAX_ENTRY_BYTES:
        raise LedgerError(
            f"entry is {size} bytes, over the {MAX_ENTRY_BYTES}-byte ceiling",
            code=CODE_ENTRY_TOO_LARGE,
            field="data",
        )
    return line


def require_data(data: Any) -> dict[str, Any]:
    """*data* unchanged, or raise ``bad_data``."""
    if not isinstance(data, dict):
        raise LedgerError(
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
    """Where a session hangs off its owning crew's ledger."""

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
        raise LedgerError(
            f"session thread must be null or {{crew, seq}}: {raw!r}",
            code=CODE_BAD_HEADER_FIELD,
            field="thread",
        )


@dataclass(frozen=True)
class CrewHeader:
    """Line 1 of a crew ledger: which unit this file belongs to, and since when.

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
class SessionHeader:
    """Line 1 of a session ledger."""

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


Header = CrewHeader | SessionHeader

_REQUIRED_HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CREW: (),
    KIND_SESSION: ("owner", "agent"),
}

_OPTIONAL_HEADER_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CREW: (),
    KIND_SESSION: ("task", "pack", "slot", "thread", "cwd", "remote"),
}

_STR_HEADER_FIELDS = frozenset({"owner", "agent", "task", "pack", "slot", "cwd"})


def build_header(kind: str, unit_id: str, created_at: int, fields: dict[str, Any]) -> Header:
    """The header for a new ledger, or raise ``bad_header_field``.

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
        raise LedgerError(
            f"unknown header field(s) for a {kind} ledger: {unknown}; allowed: {sorted(allowed)}",
            code=CODE_BAD_HEADER_FIELD,
            field=unknown[0],
        )
    missing = [name for name in _REQUIRED_HEADER_FIELDS[kind] if fields.get(name) is None]
    if missing:
        raise LedgerError(
            f"missing required header field(s) for a {kind} ledger: {missing}",
            code=CODE_BAD_HEADER_FIELD,
            field=missing[0],
        )
    for name in sorted(set(fields) & _STR_HEADER_FIELDS):
        value = fields[name]
        if value is not None and not isinstance(value, str):
            raise LedgerError(
                f"header field {name!r} must be a string or null, got {type(value).__name__}",
                code=CODE_BAD_HEADER_FIELD,
                field=name,
            )
    if kind == KIND_CREW:
        return CrewHeader(id=unit_id, created_at=created_at)
    remote = fields.get("remote")
    if remote is not None and not isinstance(remote, dict):
        raise LedgerError(
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
    ledger whose header names a different unit is not this unit's ledger, and
    reading it as if it were would attribute one unit's history to another.
    """
    if not isinstance(raw, dict):
        raise LedgerError("ledger header is not an object", code=CODE_BAD_HEADER, field="type")
    stored_kind = raw.get("type")
    if stored_kind != kind:
        raise LedgerError(
            f"ledger header says type {stored_kind!r}, opened as {kind!r}",
            code=CODE_BAD_HEADER,
            field="type",
        )
    if raw.get("id") != unit_id:
        raise LedgerError(
            f"ledger header says id {raw.get('id')!r}, opened as {unit_id!r}",
            code=CODE_BAD_HEADER,
            field="id",
        )
    created_at = raw.get("createdAt")
    if not isinstance(created_at, int) or isinstance(created_at, bool):
        raise LedgerError(
            f"ledger header createdAt must be epoch ms: {created_at!r}",
            code=CODE_BAD_HEADER,
            field="createdAt",
        )
    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise LedgerError(
            f"ledger header version must be an int: {version!r}",
            code=CODE_BAD_HEADER,
            field="version",
        )
    if kind == KIND_CREW:
        return CrewHeader(id=unit_id, created_at=created_at, version=version)
    agent = raw.get("agent")
    owner = raw.get("owner")
    if not isinstance(agent, str) or not isinstance(owner, str):
        raise LedgerError(
            f"session ledger header needs string owner and agent: {owner!r}, {agent!r}",
            code=CODE_BAD_HEADER,
            field="agent",
        )
    thread_raw = raw.get("thread")
    try:
        thread = SessionThread.coerce(thread_raw)
    except LedgerError:
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
