"""Structured lifecycle event log (parallel, additive track).

Public surface:

- :mod:`kiro_crew.events.base` — envelope, typed registry, serialize/parse
- :mod:`kiro_crew.events.kinds` — the registered event types
- :mod:`kiro_crew.events.log` — append-only daily-shard writer + prune
- :mod:`kiro_crew.events.reader` — watermark-based incremental reader
- :mod:`kiro_crew.events.backfill` — read-only validator over existing stores

No existing store is modified by anything in this package; see base.py's
module docstring for the schema contract.
"""

from __future__ import annotations

from kiro_crew.events import kinds as kinds  # noqa: F401  (registers event types)
from kiro_crew.events.base import (
    REGISTRY,
    Event,
    Parsed,
    RawEvent,
    TraceCtx,
    kind_of,
    parse,
    register,
    serialize,
)
from kiro_crew.events.log import EventLog, default_events_dir
from kiro_crew.events.reader import EventReader, ReadItem, Watermark

__all__ = [
    "REGISTRY",
    "Event",
    "EventLog",
    "EventReader",
    "Parsed",
    "RawEvent",
    "ReadItem",
    "TraceCtx",
    "Watermark",
    "default_events_dir",
    "kind_of",
    "parse",
    "register",
    "serialize",
]
