"""Append-only crew logs for crews and sessions -- the storage layer, nothing else.

The format, the rules and how this stream relates to the lifecycle event track are
specified in ``docs/system-specs/modules/crew-log-core.md``. Two units, one file
each, the gateway the only writer::

    from kiro_crew.crew_log import CrewLog

    crew = CrewLog.create("crew", "qa")
    crew.append("member/joined", {"who": "s-7f3a"}, src="gateway")

A crew header carries nothing but the unit's id and its creation time, which is
why the example passes neither: a display name belongs to the members store, which
owns it and can change it, and ``create`` refuses an unknown header field with
``bad_header_field`` rather than dropping it.

This package holds no dashboard, route, tool or migration code. It reads and
writes its own files and imports only path, lock and containment primitives, so
a consumer can be added without this layer learning about it.

The public names are re-exported LAZILY (:pep:`562`): importing the package, or
one of its submodules such as ``emit``, does not pull in ``store``, ``schema`` or
``lease``. Those load on first attribute access -- when a call actually reaches
storage -- so the flag-off gateway boot path pays for none of it.

A re-exported name lives in exactly one place, the submodule that defines it.
Reading it through this package reads that submodule, and writing it through this
package writes that submodule, so the two spellings of a name cannot hold
different values.
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import TYPE_CHECKING, Any

#: Public name -> submodule that defines it. Attribute access loads the submodule
#: on demand, so the storage layer stays unloaded until a caller reaches it.
_EXPORTS: dict[str, str] = {
    # entry_types
    "CREW_ENTRY_TYPES": "entry_types",
    "ENTRY_TYPES": "entry_types",
    "SESSION_ENTRY_TYPES": "entry_types",
    "EntryType": "entry_types",
    "Field": "entry_types",
    "declaration_for": "entry_types",
    "render_markdown": "entry_types",
    "validate_data": "entry_types",
    # errors
    "CODE_ALREADY_EXISTS": "errors",
    "CODE_ALREADY_OWNED": "errors",
    "CODE_BAD_DATA": "errors",
    "CODE_BAD_DATA_FIELD": "errors",
    "CODE_BAD_HEADER": "errors",
    "CODE_BAD_HEADER_FIELD": "errors",
    "CODE_BAD_KIND": "errors",
    "CODE_BAD_REF": "errors",
    "CODE_BAD_ROOT": "errors",
    "CODE_BAD_SEGMENT": "errors",
    "CODE_BAD_SRC": "errors",
    "CODE_BAD_THREAD": "errors",
    "CODE_BAD_TYPE": "errors",
    "CODE_ENTRY_TOO_LARGE": "errors",
    "CODE_EVENT_TYPE_NOT_OWNED": "errors",
    "CODE_INVALID_ID": "errors",
    "CODE_NAMESPACE_VIOLATION": "errors",
    "CODE_NO_LEDGER": "errors",
    "CODE_SEGMENT_GAP": "errors",
    "CODE_UNKNOWN_ENTRY_TYPE": "errors",
    "CODE_UNSUPPORTED_VERSION": "errors",
    "IndeterminateAppend": "errors",
    "CrewLogError": "errors",
    # schema
    "FIXED_SOURCES": "schema",
    "KIND_CREW": "schema",
    "KIND_FIXED_SOURCES": "schema",
    "KIND_SESSION": "schema",
    "KIND_SOURCE_PREFIXES": "schema",
    "KINDS": "schema",
    "MAX_ENTRY_BYTES": "schema",
    "MAX_REF_SPAN": "schema",
    "SCHEMA_VERSION": "schema",
    "TYPE_OWNERSHIP": "schema",
    "CrewHeader": "schema",
    "Entry": "schema",
    "Header": "schema",
    "Ref": "schema",
    "SessionHeader": "schema",
    "SessionThread": "schema",
    # store
    "DEFAULT_PAGE_LIMIT": "store",
    "LOG_FILE": "store",
    "MAX_PAGE_LIMIT": "store",
    "STATUS_CORRUPT": "store",
    "STATUS_GONE": "store",
    "STATUS_OK": "store",
    "STATUS_PRUNED": "store",
    "CrewLog": "store",
    "Page": "store",
    "Resolution": "store",
    "crew_log_dir": "store",
    "crew_log_path": "store",
    "crew_log_root": "store",
    "now_ms": "store",
    "segment_first_seqs": "store",
    "segment_paths": "store",
}

__all__ = sorted(_EXPORTS)

#: Submodule name -> the imported module, filled on first use. This caches the
#: IMPORT and never the value: a re-exported name is read from its owner on every
#: access, which is what keeps the owner the only place the value lives.
_OWNERS: dict[str, ModuleType] = {}


def _owner(name: str) -> ModuleType:
    """Return the submodule that defines ``name``, importing it on first use."""
    module = _EXPORTS[name]
    owner = _OWNERS.get(module)
    if owner is None:
        owner = _OWNERS[module] = importlib.import_module(f"{__name__}.{module}")
    return owner


def __getattr__(name: str) -> Any:
    """Read a public name from the submodule that owns it (:pep:`562`)."""
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(_owner(name), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS))


class _ReExportModule(ModuleType):
    """Send a write to a re-exported name to the submodule that owns it.

    Binding the name in this package's own namespace instead would shadow the
    owner permanently, because ``__getattr__`` runs only for a name the package
    does not already hold: the shadow would win every later read, and the owner's
    value would become unreachable through this package.

    That makes such a write undoable, which matters for the restore-by-reassign
    protocol a test harness uses (``pytest``'s ``monkeypatch`` reads the attribute
    to remember it, then assigns the remembered value back). Against a shadowing
    write, the value it reads is whatever the owner holds AT THAT MOMENT -- so a
    harness that patches the owner first remembers the patched value, and its
    restore installs that value in the package for the life of the process.
    Forwarding the write leaves one value to remember and one to put back.
    """

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _EXPORTS:
            setattr(_owner(name), name, value)
        else:
            super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if name in _EXPORTS:
            delattr(_owner(name), name)
        else:
            super().__delattr__(name)


sys.modules[__name__].__class__ = _ReExportModule


if TYPE_CHECKING:  # keep the names visible to type checkers and IDEs
    from kiro_crew.crew_log.entry_types import (  # noqa: F401
        CREW_ENTRY_TYPES,
        ENTRY_TYPES,
        SESSION_ENTRY_TYPES,
        EntryType,
        Field,
        declaration_for,
        render_markdown,
        validate_data,
    )
    from kiro_crew.crew_log.errors import (  # noqa: F401
        CODE_ALREADY_EXISTS,
        CODE_ALREADY_OWNED,
        CODE_BAD_DATA,
        CODE_BAD_DATA_FIELD,
        CODE_BAD_HEADER,
        CODE_BAD_HEADER_FIELD,
        CODE_BAD_KIND,
        CODE_BAD_REF,
        CODE_BAD_ROOT,
        CODE_BAD_SEGMENT,
        CODE_BAD_SRC,
        CODE_BAD_THREAD,
        CODE_BAD_TYPE,
        CODE_ENTRY_TOO_LARGE,
        CODE_EVENT_TYPE_NOT_OWNED,
        CODE_INVALID_ID,
        CODE_NAMESPACE_VIOLATION,
        CODE_NO_LEDGER,
        CODE_SEGMENT_GAP,
        CODE_UNKNOWN_ENTRY_TYPE,
        CODE_UNSUPPORTED_VERSION,
        CrewLogError,
        IndeterminateAppend,
    )
    from kiro_crew.crew_log.schema import (  # noqa: F401
        FIXED_SOURCES,
        KIND_CREW,
        KIND_FIXED_SOURCES,
        KIND_SESSION,
        KIND_SOURCE_PREFIXES,
        KINDS,
        MAX_ENTRY_BYTES,
        MAX_REF_SPAN,
        SCHEMA_VERSION,
        TYPE_OWNERSHIP,
        CrewHeader,
        Entry,
        Header,
        Ref,
        SessionHeader,
        SessionThread,
    )
    from kiro_crew.crew_log.store import (  # noqa: F401
        DEFAULT_PAGE_LIMIT,
        LOG_FILE,
        MAX_PAGE_LIMIT,
        STATUS_CORRUPT,
        STATUS_GONE,
        STATUS_OK,
        STATUS_PRUNED,
        CrewLog,
        Page,
        Resolution,
        crew_log_dir,
        crew_log_path,
        crew_log_root,
        now_ms,
        segment_first_seqs,
        segment_paths,
    )
