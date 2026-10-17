"""How an omitted section key gets its value: ``SectionReader`` and the named defaults.

``SectionReader`` resolves a key a section omits, and a coercer's fallback, to the default
declared on the DTO field. The named defaults (``DEFAULT_MODEL``, ``DEFAULT_POOL_SIZE``, ...)
are the values a DTO field and a load fallback both read. Every other section owner sits
above this one.

An owner of the :mod:`kiro_crew.config.sections` facade; it never imports the facade.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar

from kiro_crew.config.fields import _declared_fields, field_default

_T = TypeVar("_T")
_ABSENT = object()


class SectionReader:
    """Read one ``config.json`` section against its DTO's declared field defaults.

    The one rule it owns: a key the section OMITS resolves to the default declared on
    the DTO's field, and a coercer handed a value it cannot read falls back to that
    same default. A builder therefore states no default of its own. A second
    spelling of one default in a builder could drift from the field and answer the
    opposite value, silently, for exactly the installs whose ``config.json`` predates
    the key -- while a home with no config file builds the bare dataclass and gets the
    field's value.

    * A PRESENT key is returned as stored, ``None`` included: only absence takes the
      default.
    * A ``default_factory`` field yields a fresh value on every read, so a built
      section never shares a mutable default with another load.
    * A key that names no field of the DTO raises ``KeyError``: it has no default to
      fall back to.
    """

    __slots__ = ("_data", "_dto", "_fields")

    def __init__(self, dto: type, data: Mapping[str, Any]) -> None:
        self._data = data
        self._dto = dto
        self._fields = _declared_fields(dto)

    def default(self, key: str) -> Any:
        """The default declared on *key*'s field (a fresh one for a factory field)."""
        return field_default(self._dto, key)

    def get(self, key: str) -> Any:
        """The stored value of *key*, or its field's default when the section omits it."""
        if key not in self._fields:
            raise KeyError(key)
        value = self._data.get(key, _ABSENT)
        return self.default(key) if value is _ABSENT else value

    def read(self, key: str, coerce: Callable[..., _T], *bounds: Any, **options: Any) -> _T:
        """``coerce(stored-or-default, default, *bounds, **options)`` for *key*.

        For the ``_safe_*`` coercer family, whose second argument is the fallback for
        a value they cannot read: the fallback is the field's default too.
        """
        return coerce(self.get(key), self.default(key), *bounds, **options)


DEFAULT_MODEL = "auto"
DEFAULT_SESSION_TIMEOUT = 3600  # 60 min
# Auto-compaction threshold, as a percentage of the context window. Named
# because two code paths need it — the dataclass field default (used only when
# there is no config file) and the dict-load fallback in ``load()`` (used when
# a config file omits the key). Restating the number in both lets them disagree
# with nothing on disk to show it, which is why ``pool_size`` is named the same
# way (``DEFAULT_POOL_SIZE``) rather than written twice.
DEFAULT_AUTOCOMPACT_PCT = 70.0
# Margin BELOW the configured compaction threshold at which the "context is
# getting large" warning fires. A margin rather than an absolute percentage
# because both consumers test compaction FIRST in an if/elif chain
# (``session.check_context_usage`` and the ``cli_chat`` REPL loop), so an
# absolute warn level at or above the configured threshold makes the warning arm
# unreachable and the early signal disappears for whoever did not change the
# default. Kept here rather than in either consumer so the two cannot drift.
#
# 10 points, so the warning carries one fixed meaning — "within 10 points of
# compaction" — whatever threshold the operator configures. Width is what makes
# the signal readable: at 20 the warning covers the top 20 of the 70 usable
# points on the default threshold and fires on every turn from half the context
# window onward, which is where an always-on warning stops being read.
# ``test_the_warning_stays_a_minority_of_the_usable_range`` holds the band under
# a quarter of the range so it cannot widen back into noise.
CONTEXT_WARN_MARGIN_PCT = 10.0
# session.pool_size — warm pool OFF by default. Each pooled slot is a full
# kiro-cli process plus the MCP stdio servers its agent spec spawns (~109 MB per
# backend), and a non-zero value is also reserved out of the memory term that
# sizes the TaskRunner's auto parallel cap
# (subagent.compute_memory_sized_parallel_cap), so the cost is paid on every host
# whether or not the pool is ever claimed. Cold start is instead
# hidden by session.eager_spawn, which is on by default and pre-creates a slot's
# session behind user think-time.
#
# Read by BOTH the SessionConfig field default and load()'s file-parse fallback,
# because those are two independent paths to the same value: a home with no
# config.json takes the field default, and a config.json that omits the key takes
# the parse fallback. A literal in either place lets the two disagree, which is
# invisible on disk — this constant is the only place the value is written.
DEFAULT_POOL_SIZE = 0
# Per-session process-tree RSS ceiling (MiB) the cleanup watchdog recycles an
# idle session at. 0 disables, and that is the default: a fixed ceiling cannot
# tell a leak from a healthy session that loads many MCP servers. An agent with
# six MCP servers measured ~1.4 GB of tree RSS thirteen seconds after start, so
# the old 1536 default recycled ordinary sessions after one heavy turn. An
# operator who wants a bound sets one sized to their own agents.
DEFAULT_WATCHDOG_RSS_MAX_MB = 0
# session.reconcile_max_kills — root candidates the runtime reconciler may signal
# the tree of in one pass. Defaults to the budget the arm already ships with, so an
# unconfigured host behaves exactly as before; the field's ceiling equals that same
# value, which makes the knob purely SUBTRACTIVE -- it can withhold signals and
# cannot authorize any the product does not already authorize.
#
# Subtractive on purpose, because the value governs host-side signals and
# ``config.json`` is agent-writable and never passes the dashboard's write gate. A
# knob whose reachable range sat above the shipped default would let a write turn
# killing up; this one cannot.
#
# An operator needs to turn it DOWN because the arm's evidence of abandonment is
# "no record on this data home claims this pid", and that evidence is only as wide
# as the records one process can read. The agent slice is named from a hash of the
# config directory, so every install sharing a data home shares the slice, and a
# runtime whose owner is a different process is claimed only by records that
# process holds. Measured on such a host: 250-504 unowned pids per pass against 4
# genuine strays in 6.5 hours. Setting 0 takes the reading without the signal.
DEFAULT_RECONCILE_MAX_KILLS = 5
