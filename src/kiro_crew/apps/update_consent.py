"""Which permissions a new app version asks for that the installed one did not.

An automatic update must never be the moment an app gains a capability. The
manual Update path handles widening in two ways of its own: added
``permissions.api`` / ``permissions.events`` entries are held back until the
owner approves them, and an added ``permissions.sessionApproval`` turns the app
off until it is re-enabled. Every other permission takes effect with the new
manifest. An update nobody pressed has no consent moment at all, so the
automatic path refuses any widening before files move and asks instead.

Pure functions over manifest permissions, so the refusal can be computed both
before a registry clone (from the prefetched manifest, to skip work that would
be thrown away) and again inside ``update_app`` (from the copied manifest,
which is the one that would actually run).
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Iterator

#: Error code an update refused for new consent answers with. The automatic
#: updater keys its "Ask" fallback on it; the route returns it unchanged.
NEEDS_CONSENT_CODE = "app_update_needs_consent"

#: Error code an automatic update answers with when the source it would copy
#: is not newer than what is installed. The index row can advertise a version
#: the clone does not carry; installing it would be a silent downgrade.
NOT_NEWER_CODE = "app_update_not_newer"

_LIST_FIELDS = ("api", "events", "mcpTools", "exposeToApps")
_BOOL_FIELDS = ("storage", "network", "cron", "sessionApproval", "spawn", "jobs", "proposeCrewmate")
#: ``permissions.memory`` from narrowest to widest.
_MEMORY_RANK = {"": 0, "app-scoped": 1, "shared": 2}


def _get(permissions: Any, key: str) -> Any:
    if isinstance(permissions, dict):
        return permissions.get(key)
    return getattr(permissions, key, None)


def _strings(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {v for v in value if isinstance(v, str) and v}


def widened_permissions(old: Any, new: Any) -> list[str]:
    """Names of the permissions *new* grants beyond *old*, in a stable order.

    *old* and *new* are ``Permissions`` objects or raw manifest dicts. ``None``
    for *old* means the installed manifest could not be read, so nothing is
    known to be approved and every permission *new* declares counts as added.
    A ``memory`` value outside the known scale counts as widened whenever it
    differs, since its meaning cannot be ranked. An ``mcpTools`` list that goes
    from some entries to none counts as widened, because an empty list means
    the app is not limited to named tools.
    """
    if new is None:
        return []
    widened: list[str] = []
    for key in _LIST_FIELDS:
        new_entries = _strings(_get(new, key))
        old_entries = set() if old is None else _strings(_get(old, key))
        if key == "mcpTools" and old is not None:
            # An empty ``mcpTools`` is not "no tools": ``managed_tool_exclusion``
            # reads it as no restriction. So emptying a list widens access, and
            # going from empty to a named list narrows it.
            if old_entries and (not new_entries or new_entries - old_entries):
                widened.append(key)
        elif new_entries - old_entries:
            widened.append(key)
    for key in _BOOL_FIELDS:
        if _get(new, key) is True and (old is None or _get(old, key) is not True):
            widened.append(key)
    new_memory = _get(new, "memory") or ""
    old_memory = "" if old is None else (_get(old, "memory") or "")
    if new_memory != old_memory:
        new_rank = _MEMORY_RANK.get(new_memory) if isinstance(new_memory, str) else None
        old_rank = _MEMORY_RANK.get(old_memory) if isinstance(old_memory, str) else None
        if new_rank is None or old_rank is None or new_rank > old_rank:
            widened.append("memory")
    return widened


#: The refusal for an automatic update whose installed manifest cannot be read.
#: Without it nothing is known to be approved, and a new version that declares
#: less (an ``mcpTools`` list dropped to empty means unrestricted) would compare
#: as no widening, so the owner decides instead.
UNREADABLE_BASELINE_REFUSAL = (
    "the installed app's manifest could not be read, so the permissions the new "
    "version asks for cannot be compared with what was approved"
)


def consent_refusal(widened: list[str]) -> str:
    """The error text for an update held back for *widened* permissions."""
    return "the new version asks for permissions this app does not have yet: " + ", ".join(widened)


# The scope an automatic update runs in. A ContextVar rather than a parameter
# because the registry pipeline reaches ``update_app`` several calls down, and
# ``asyncio.to_thread`` copies the context into its worker, so the check at the
# replacement boundary sees the same answer the caller set.
_REFUSE_WIDENING: ContextVar[bool] = ContextVar("app_update_refuse_widening", default=False)


@contextmanager
def refusing_permission_widening() -> Iterator[None]:
    """Within this block, an app update that widens permissions is refused."""
    token = _REFUSE_WIDENING.set(True)
    try:
        yield
    finally:
        _REFUSE_WIDENING.reset(token)


def permission_widening_refused() -> bool:
    """Whether the current update runs inside :func:`refusing_permission_widening`."""
    return _REFUSE_WIDENING.get()
