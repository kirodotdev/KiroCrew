"""Structured lifecycle event schema (parallel, additive track).

Public surface:

- :mod:`kiro_crew.events.base` — envelope, typed registry, serialize/parse
- :mod:`kiro_crew.events.kinds` — the registered event types
- :mod:`kiro_crew.events.backfill` — read-only validator over existing stores

This package is the SCHEMA and its production-data proof, nothing more. An
on-disk store for THIS envelope (writer, watermark reader, retention) waits for
an unsequenced fact whose emitter needs it; the additive-only rule makes adding
it later free. It is not where an ordered fact goes: the decision of record is
``docs/request-for-change/rfc-append-only-ledger.md``, which puts facts needing
order, threading or citation on the per-unit append-only ledger in
:mod:`kiro_crew.ledger`, with a writer that assigns a per-unit ``seq``. That envelope is field-compatible with this one -- ``type`` for
``kind``, ``time`` for ``ts_ms`` -- so one projection folds both with a field
rename. Nothing here modifies an existing store; see base.py's module docstring
for the schema contract.
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
