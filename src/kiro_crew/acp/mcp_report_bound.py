"""THE cut for a session's MCP report: what a bounded list keeps, and how many it leaves out.

A session's report (:mod:`kiro_crew.acp.mcp_session_report`) keeps a bounded
number of server names in each of its lists, and a host or a spec chooses how
many it is handed. Every list is cut by :func:`keep` and by nothing else, so a
cut always comes with its count: a cut tail otherwise reads exactly like servers
nobody ever named, and a server the session expects whose entry was cut would
read as still starting for as long as the session lives. The report records
each count (``McpSessionReport.left_out``) and stops reading "no report" as
"still starting" once any list left a name out.

:func:`read_status_snapshot` is the one reader of a host status snapshot
(``_kiro/mcp/status``): the host's whole list of servers. Only what the report
keeps of an entry leaves it, each field bounded: a cleaned name, one of the
statuses the report tells apart, and a cleaned error.

Nothing here imports the report. It is handed the report's bound and its
cleaners, so the two cannot drift and there is no import cycle.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

_T = TypeVar("_T", bound=Hashable)

#: The statuses a report tells apart. Any other status, or none, is a server
#: that has not settled.
_SETTLED = ("connected", "failed", "disabled")


def keep(items: Iterable[_T], cap: int) -> tuple[tuple[_T, ...], int]:
    """The first *cap* different *items*, in order, and how many more it was handed.

    A repeat of a kept item is not counted; every other item past *cap* is,
    once each time it comes. Nothing past *cap* is held, so *items* may be as
    long as its sender likes.
    """
    kept: dict[_T, None] = {}
    left_out = 0
    for item in items:
        if item in kept:
            continue
        if len(kept) < cap:
            kept[item] = None
        else:
            left_out += 1
    return tuple(kept), left_out


@dataclass(frozen=True)
class StatusSnapshot:
    """What a report reads of one status snapshot."""

    #: ``(name, status, needs a sign-in, error)`` for each server read, in the
    #: order the host first lists them: one row a server, its LAST entry.
    #: *status* is one of :data:`_SETTLED`, or ``""`` for a server that has not
    #: settled.
    servers: tuple[tuple[str, str, bool, str], ...]
    #: The servers read that the host calls disabled.
    disabled: set[str]
    #: The servers read that the host lists as not settled (connecting, or
    #: connecting again).
    pending: set[str]
    #: The servers the host's registry dropped instead of starting.
    dropped: tuple[str, ...]
    #: How many entries the snapshot lists past the bound, in either list. They
    #: were not read.
    omitted: int


def read_status_snapshot(
    params: Any,
    *,
    cap: int,
    dropped_fields: Iterable[str],
    clean_name: Callable[[str], str],
    clean_error: Callable[[str], str],
) -> StatusSnapshot | None:
    """Read the status snapshot in *params*; ``None`` when it carries no server list.

    The first *cap* different servers the host lists are read, each by the
    LAST entry the host gives it, wherever in the list that is: a server read
    here never keeps a state the host has since replaced. So are the first
    *cap* registry-dropped names (the lists under *dropped_fields*).
    *clean_name* makes a name the one the report compares, or ``""``;
    *clean_error* bounds an entry's error text. Every entry that names another
    server is counted in ``omitted``, and so is every dropped name past *cap*.
    """
    servers = params.get("servers") if isinstance(params, dict) else None
    if not isinstance(servers, list):
        return None

    def entries() -> Iterable[tuple[str, str, bool, str]]:
        for entry in servers:
            name = clean_name(str(entry.get("name") or "")) if isinstance(entry, dict) else ""
            if not name:
                continue
            status = entry.get("status")
            status = status if isinstance(status, str) and status in _SETTLED else ""
            error = clean_error(str(entry.get("errorMessage") or ""))
            yield name, status, entry.get("failedAuthorization") is True, error

    def dropped_names() -> Iterable[str]:
        for key in dropped_fields:
            items = params.get(key)
            for item in items if isinstance(items, list) else ():
                raw = item.get("name") if isinstance(item, dict) else item
                name = clean_name(raw) if isinstance(raw, str) else ""
                if name:
                    yield name

    names, more = keep((entry[0] for entry in entries()), cap)
    read_names = set(names)
    last = {entry[0]: entry for entry in entries() if entry[0] in read_names}
    read = tuple(last[name] for name in names)
    dropped, more_dropped = keep(dropped_names(), cap)
    disabled = {name for name, status, _, _ in read if status == "disabled"}
    pending = {name for name, status, _, _ in read if not status}
    return StatusSnapshot(read, disabled, pending, dropped, more + more_dropped)
