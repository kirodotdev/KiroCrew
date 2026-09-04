"""Typed envelope and registry for the structured lifecycle event log.

This package is a **parallel, additive** track: it does not read from, write
to, or import any existing store (transcripts, usage shards, SEL, cron or
autonudge snapshots). Those stores stay authoritative; this log records
lifecycle *facts* — small, structured, append-only — so later consumers
(projections such as a task manager or the context-breakdown panel) can fold
them into state without bespoke store-to-panel pipelines.

Schema contract (v1):

- Every line is one JSON object: ``{"v": 1, "kind": "<domain>/<action>",
  "src": "<emitting subsystem>", "key": "<correlation key>",
  "ts_ms": <int>, "data": {...}}``.
- ``kind`` is namespaced ``domain/action`` and owned by exactly one registered
  :class:`Event` subclass.
- ``key`` is the cross-domain join axis. It is an OPAQUE correlation string,
  unique within its kind's domain, and its shape is NOT load-bearing: what an
  event is about is read from ``kind``, which every event already carries.
  By convention a non-session entity is written ``<domain>:<id>``
  (``cron:daily-report``, ``subagent:ab12``, ``nudge:chat-3``) and a session is
  written with the identifier the rest of the codebase already uses for it
  (``chat-31``, and ``slack:1712793600.123`` for a Slack thread). That last one
  is why shape cannot classify: a session key may itself contain a colon, so
  "has a prefix" does not mean "is not a session". Deliberately there is NO
  registry of entity domains, because defining "session" as the complement of an
  open prefix list would let a future domain silently reclassify existing keys --
  the ``kind`` field already answers the question without that hazard.
- There is deliberately NO sequence/ordering field yet. Ordering is the axis
  consumers will fold on, so it needs a defined scope (per writer? per key?
  global?) and a writer that assigns it -- neither of which exists while
  nothing emits. It arrives, specified and exercised, with the first emitter;
  ``ts_ms`` carries the event's own time meanwhile.
- Evolution is **additive only**: new kinds, new optional ``data`` fields, and
  new optional ENVELOPE keys may be added; existing fields are never renamed or
  repurposed. ``v`` exists as the escape hatch for a future incompatible break,
  not as a versioning workflow.
- Parsing is tolerant by construction: an unknown ``kind`` (or a known kind
  whose required fields cannot be satisfied) parses as :class:`RawEvent`
  instead of raising, and an unrecognised envelope key is ignored rather than
  rejected -- so an old reader never chokes on a newer writer.
"""

from __future__ import annotations

import json
import logging
import types
from dataclasses import dataclass, field
from dataclasses import fields as dc_fields
from typing import Any, ClassVar, Union, get_args, get_origin, get_type_hints

logger = logging.getLogger(__name__)

#: Envelope schema version. Bumped only for an incompatible break (see module
#: docstring); additive changes never bump it.
SCHEMA_VERSION = 1

#: Base-envelope attribute names — everything else on a subclass is ``data``.
_BASE_FIELDS = frozenset({"key", "ts_ms"})

_HINTS_CACHE: dict[type, dict[str, Any]] = {}


def _field_ok(value: Any, tp: Any) -> bool:
    """True when *value* satisfies annotation *tp* (scalars and unions).

    ``bool`` is rejected for int/float fields even though it subclasses
    ``int`` — a JSON ``true`` in a numeric field is corruption, not a count.
    Unrecognized annotation shapes pass rather than block.
    """
    origin = get_origin(tp)
    if origin is Union or isinstance(tp, types.UnionType):
        return any(_field_ok(value, a) for a in get_args(tp))
    if tp is type(None):
        return value is None
    if tp is Any:
        return True
    if isinstance(tp, type):
        if tp is int:
            return isinstance(value, int) and not isinstance(value, bool)
        if tp is float:
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        return isinstance(value, tp)
    return True


def _payload_matches(cls: type, kwargs: dict[str, Any]) -> bool:
    hints = _HINTS_CACHE.get(cls)
    if hints is None:
        try:
            hints = get_type_hints(cls)
        except Exception:
            hints = {}
        _HINTS_CACHE[cls] = hints
    return all(_field_ok(val, hints.get(name, Any)) for name, val in kwargs.items())


@dataclass(frozen=True)
class Event:
    """Base event: correlation key + timestamp.

    Subclasses declare ``KIND`` (``domain/action``) and typed fields; those
    extra fields serialize under the envelope's ``data`` object.
    """

    KIND: ClassVar[str] = ""

    #: Correlation key, e.g. ``chat-31``, ``cron:daily-report``, ``subagent:ab12``.
    key: str
    #: Event time, epoch milliseconds (UTC).
    ts_ms: int

    def data(self) -> dict[str, Any]:
        """Typed fields beyond the base envelope, for serialization."""
        return {
            f.name: getattr(self, f.name) for f in dc_fields(self) if f.name not in _BASE_FIELDS
        }


@dataclass(frozen=True)
class RawEvent(Event):
    """Forward-compatibility fallback: an event this build cannot type.

    Produced by :func:`parse` for an unknown ``kind`` or a known kind whose
    payload does not satisfy the typed constructor. Carries the wire ``kind``
    and the raw ``data`` payload untouched so nothing is lost round-tripping.
    """

    KIND: ClassVar[str] = ""

    kind: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def data(self) -> dict[str, Any]:
        return dict(self.payload)


#: kind -> registered Event subclass. Populated by :func:`register`.
REGISTRY: dict[str, type[Event]] = {}


def register(cls: type[Event]) -> type[Event]:
    """Class decorator: validate ``KIND`` and add the class to the registry.

    ``KIND`` must be lowercase ``domain/action`` (exactly one slash) and unique
    across the process. Violations raise at import time — a malformed kind is
    a programming error, not a runtime condition.
    """
    kind = cls.KIND
    # ``bool(action)`` already carries "a separator was present": with no "/"
    # in *kind*, partition puts the whole string in domain and leaves BOTH the
    # separator and the tail empty, so a non-empty action is only reachable
    # once the slash was found. ``"/" not in action`` is a different and still
    # necessary test — it is what forbids a SECOND slash, since partition
    # splits at the first one.
    domain, _, action = kind.partition("/")
    valid = bool(domain) and bool(action) and "/" not in action and kind == kind.lower()
    if not valid:
        raise ValueError(f"event KIND must be lowercase 'domain/action', got {kind!r}")
    existing = REGISTRY.get(kind)
    if existing is not None and existing is not cls:
        raise ValueError(f"event KIND {kind!r} already registered by {existing.__name__}")
    REGISTRY[kind] = cls
    return cls


def kind_of(event: Event) -> str:
    """The wire ``kind`` for *event* (RawEvent carries it per-instance)."""
    if isinstance(event, RawEvent):
        return event.kind
    return type(event).KIND


def serialize(event: Event, *, src: str) -> str:
    """One compact JSON line for *event* under the v1 envelope."""
    rec: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "kind": kind_of(event),
        "src": src,
        "key": event.key,
        "ts_ms": event.ts_ms,
        "data": event.data(),
    }
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Parsed:
    """A parsed line: the (typed or raw) event plus envelope metadata."""

    event: Event
    kind: str
    src: str
    v: int


def parse(line: str) -> Parsed | None:
    """Parse one log line. Returns ``None`` only for non-JSON / non-object input.

    Guarantees for well-formed envelopes: never raises. A ``kind`` this build
    does not know — or a known kind whose payload violates a field's declared
    type, or leaves a required field absent — yields a :class:`RawEvent`
    carrying the payload verbatim. Unknown fields inside ``data`` for a known
    kind are ignored (additive evolution).
    """
    try:
        rec = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rec, dict):
        return None
    kind = rec.get("kind")
    key = rec.get("key")
    ts_ms = rec.get("ts_ms")
    # bool subclasses int, so isinstance alone would accept "ts_ms": true
    # and timestamp the event at epoch millisecond 1.
    if (
        not isinstance(kind, str)
        or not isinstance(key, str)
        or not isinstance(ts_ms, int)
        or isinstance(ts_ms, bool)
    ):
        return None
    src_val = rec.get("src")
    src = src_val if isinstance(src_val, str) else ""
    v_val = rec.get("v")
    v = v_val if isinstance(v_val, int) and not isinstance(v_val, bool) else 0
    # An ABSENT data key is a legitimately empty payload; a PRESENT non-dict
    # value — explicit null included — violates the envelope contract (the
    # serializer always writes a dict), and substituting {} would hand
    # consumers a typed event with invented defaults. Corrupt like a bad
    # ts_ms. The get() default fires only on absence, which is what keeps
    # those two cases apart.
    data_val = rec.get("data", {})
    if not isinstance(data_val, dict):
        return None
    data: dict[str, Any] = data_val
    cls = REGISTRY.get(kind)
    typed: Event | None = None
    if cls is not None:
        allowed = {f.name for f in dc_fields(cls)} - _BASE_FIELDS
        kwargs = {k: val for k, val in data.items() if k in allowed}
        # A retained value that violates the field's declared type must not
        # reach consumers as a typed event — that would make the dataclass
        # schema a lie. Such lines degrade to RawEvent like unknown kinds.
        if _payload_matches(cls, kwargs):
            try:
                typed = cls(key=key, ts_ms=ts_ms, **kwargs)
            except TypeError:
                # Required typed field missing — leave ``typed`` at ``None`` so
                # the fallback below applies, rather than failing the parse.
                pass
    # One fallback for all three degrade paths: unknown kind, a payload whose
    # value violates the field's declared type, and a payload missing a
    # required typed field.
    event = typed if typed is not None else RawEvent(key=key, ts_ms=ts_ms, kind=kind, payload=data)
    return Parsed(event=event, kind=kind, src=src, v=v)
