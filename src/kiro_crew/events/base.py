"""Typed envelope and registry for the structured lifecycle event log.

This package is a **parallel, additive** track: it does not read from, write
to, or import any existing store (transcripts, usage shards, SEL, cron or
autonudge snapshots). Those stores stay authoritative; this log records
lifecycle *facts* — small, structured, append-only — so later consumers
(projections such as a task manager or the context-breakdown panel) can fold
them into state without bespoke store-to-panel pipelines.

Schema contract (v1):

- Every line is one JSON object: ``{"v": 1, "kind": "<domain>/<action>",
  "src": "<emitting subsystem>", "seq": <int>, "key": "<correlation key>",
  "ts_ms": <int>, "trace": {...}?, "data": {...}}``.
- ``kind`` is namespaced ``domain/action`` and owned by exactly one registered
  :class:`Event` subclass.
- Evolution is **additive only**: new kinds and new optional ``data`` fields
  may be added; existing fields are never renamed or repurposed. ``v`` exists
  as the escape hatch for a future incompatible break, not as a versioning
  workflow.
- Parsing is tolerant by construction: an unknown ``kind`` (or a known kind
  whose required fields cannot be satisfied) parses as :class:`RawEvent`
  instead of raising, so an old reader never chokes on a newer writer.
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
_BASE_FIELDS = frozenset({"key", "ts_ms", "trace"})

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
class TraceCtx:
    """W3C trace-context identifiers carried by an event.

    Unused by the backfill validator (it emits ``trace=None``); the field
    exists in v1 so live emitters added later can propagate a distributed
    trace (session turn -> subagent -> MCP call) without a schema break.
    """

    trace_id: str
    span_id: str
    parent_span_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"trace_id": self.trace_id, "span_id": self.span_id}
        if self.parent_span_id is not None:
            d["parent_span_id"] = self.parent_span_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TraceCtx | None":
        try:
            return cls(
                trace_id=str(d["trace_id"]),
                span_id=str(d["span_id"]),
                parent_span_id=(
                    str(d["parent_span_id"]) if d.get("parent_span_id") is not None else None
                ),
            )
        except (KeyError, TypeError):
            return None


@dataclass(frozen=True)
class Event:
    """Base event: correlation key + timestamp + optional trace context.

    Subclasses declare ``KIND`` (``domain/action``) and typed fields; those
    extra fields serialize under the envelope's ``data`` object.
    """

    KIND: ClassVar[str] = ""

    #: Correlation key, e.g. ``chat-31``, ``cron:daily-report``, ``subagent:ab12``.
    key: str
    #: Event time, epoch milliseconds (UTC).
    ts_ms: int
    trace: TraceCtx | None = None

    def data(self) -> dict[str, Any]:
        """Typed fields beyond the base envelope, for serialization."""
        return {
            f.name: getattr(self, f.name)
            for f in dc_fields(self)
            if f.name not in _BASE_FIELDS
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
    domain, sep, action = kind.partition("/")
    valid = (
        bool(sep)
        and bool(domain)
        and bool(action)
        and "/" not in action
        and kind == kind.lower()
    )
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


def serialize(event: Event, *, src: str, seq: int) -> str:
    """One compact JSON line for *event* under the v1 envelope."""
    rec: dict[str, Any] = {
        "v": SCHEMA_VERSION,
        "kind": kind_of(event),
        "src": src,
        "seq": seq,
        "key": event.key,
        "ts_ms": event.ts_ms,
    }
    if event.trace is not None:
        rec["trace"] = event.trace.to_dict()
    rec["data"] = event.data()
    return json.dumps(rec, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass(frozen=True)
class Parsed:
    """A parsed line: the (typed or raw) event plus envelope metadata."""

    event: Event
    kind: str
    src: str
    seq: int
    v: int


def parse(line: str) -> Parsed | None:
    """Parse one log line. Returns ``None`` only for non-JSON / non-object input.

    Guarantees for well-formed envelopes: never raises. A ``kind`` this build
    does not know — or a known kind whose required fields are absent — yields
    a :class:`RawEvent` carrying the payload verbatim. Unknown fields inside
    ``data`` for a known kind are ignored (additive evolution).
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
    seq_val = rec.get("seq")
    seq = seq_val if isinstance(seq_val, int) and not isinstance(seq_val, bool) else -1
    v_val = rec.get("v")
    v = v_val if isinstance(v_val, int) and not isinstance(v_val, bool) else 0
    # An ABSENT data key is a legitimately empty payload; a PRESENT non-dict
    # value — explicit null included, which rec.get() cannot distinguish
    # from absence — violates the envelope contract (the serializer always
    # writes a dict), and substituting {} would hand consumers a typed
    # event with invented defaults. Corrupt like a bad ts_ms.
    if "data" not in rec:
        data: dict[str, Any] = {}
    else:
        data_val = rec.get("data")
        if isinstance(data_val, dict):
            data = data_val
        else:
            return None
    trace_val = rec.get("trace")
    trace = TraceCtx.from_dict(trace_val) if isinstance(trace_val, dict) else None

    cls = REGISTRY.get(kind)
    event: Event
    if cls is not None:
        allowed = {f.name for f in dc_fields(cls)} - _BASE_FIELDS
        kwargs = {k: val for k, val in data.items() if k in allowed}
        # A retained value that violates the field's declared type must not
        # reach consumers as a typed event — that would make the dataclass
        # schema a lie. Such lines degrade to RawEvent like unknown kinds.
        if not _payload_matches(cls, kwargs):
            event = RawEvent(key=key, ts_ms=ts_ms, trace=trace, kind=kind, payload=data)
        else:
            try:
                event = cls(key=key, ts_ms=ts_ms, trace=trace, **kwargs)
            except TypeError:
                # Required typed field missing — fall back rather than fail.
                event = RawEvent(key=key, ts_ms=ts_ms, trace=trace, kind=kind, payload=data)
    else:
        event = RawEvent(key=key, ts_ms=ts_ms, trace=trace, kind=kind, payload=data)
    return Parsed(event=event, kind=kind, src=src, seq=seq, v=v)
