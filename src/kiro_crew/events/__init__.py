"""Structured lifecycle event schema (parallel, additive track).

Public surface:

- :mod:`kiro_crew.events.base` — envelope, typed registry, serialize/parse
- :mod:`kiro_crew.events.kinds` — the registered event types
- :mod:`kiro_crew.events.backfill` — read-only validator over existing stores

This package is the SCHEMA and its production-data proof, nothing more. The
on-disk store (writer, watermark reader, retention) lands with the first emit
site that produces events and the first consumer that folds them; the envelope's
additive-only rule makes adding those later free. Nothing here modifies an
existing store; see base.py's module docstring for the schema contract.
"""

from __future__ import annotations

from kiro_crew.events import kinds as kinds  # noqa: F401  (registers event types)
from kiro_crew.events.base import (
    REGISTRY,
    Event,
    Parsed,
    RawEvent,
    kind_of,
    parse,
    register,
    serialize,
)

__all__ = [
    "REGISTRY",
    "Event",
    "Parsed",
    "RawEvent",
    "kind_of",
    "parse",
    "register",
    "serialize",
]
