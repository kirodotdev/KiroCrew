"""One copy of the large lists the engine keeps re-sending, shared by every frame.

A KAS engine re-sends each session's whole MCP status (every server with its full
tool catalog) and its whole ``configOptions`` whenever something changes, idle
sessions included. A session reads its queue only when it runs a turn, so an idle
session keeps every such frame it was sent, and each one carries its own copy of
the same catalogs. On one long-running gateway, 14,862 tool entries held 52 MiB
with only 1,382 different ones among them.

:class:`RepeatedPayloads` swaps a list a new frame carries for an equal list the
runtime already holds, before the frame is queued. A list is held for sharing only
when it matches in type as well as value: Python reads ``True``, ``1`` and ``1.0``
as equal, and a reader of a tool schema sees three different JSON types. Only the
lists are shared:
every frame, its order, the queue's length and each field a reader compares stay
exactly as the engine sent them, so nothing that reads the queue can tell. No
reader of these lists writes to them (``absorb_config_options`` stores the list
as it is, and the MCP report and sign-in tracking only read it).
"""

from __future__ import annotations

from typing import Any

from kiro_crew.acp.types import METHOD_KAS_MCP_STATUS, METHOD_SESSION_UPDATE, JsonRpcMessage

#: The lists a status snapshot carries for each server.
_SERVER_LISTS = ("tools", "prompts", "resources", "resourceTemplates")
#: The most server names whose lists are kept; past it, a new name is not shared.
#: Far more than a host runs, so it only bounds what a misbehaving engine can grow.
MAX_SERVERS = 512
_MISSING = object()


def _same_in_type(held: Any, value: Any) -> bool:
    """Whether *held* and *value* are equal and every value has the same type.

    The engine's frames are untrusted, so a deeply nested schema must not decide how
    much stack the walk uses: it keeps its own stack of pairs and never recurses.
    """
    pending = [(held, value)]
    while pending:
        left, right = pending.pop()
        if left is right:
            continue
        kind = type(left)
        if kind is not type(right):
            return False
        if isinstance(left, dict):
            if len(left) != len(right):
                return False
            for key, item in left.items():
                other = right.get(key, _MISSING)
                if other is _MISSING:
                    return False
                pending.append((item, other))
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def _matches(held: list[Any], value: list[Any]) -> bool:
    """Whether a new list can stand in for the held one without a reader noticing."""
    try:
        if held != value:
            return False
    except RecursionError:
        pass  # too deep for the built-in comparison; the walk below decides
    return _same_in_type(held, value)


class RepeatedPayloads:
    """The last list of each kind the runtime saw, for frames to share. LOOP-AFFINE."""

    __slots__ = ("_by_server", "_config_options")

    def __init__(self) -> None:
        #: Server name -> list name (``tools``, ``prompts``, ...) -> the held list.
        self._by_server: dict[str, dict[str, list[Any]]] = {}
        self._config_options: list[Any] | None = None

    def share(self, msg: JsonRpcMessage) -> None:
        """Point *msg*'s large lists at equal ones already held, in place."""
        if msg.id is not None or not isinstance(msg.params, dict):
            return
        if msg.method == METHOD_KAS_MCP_STATUS:
            servers = msg.params.get("servers")
            if isinstance(servers, list):
                for entry in servers:
                    if isinstance(entry, dict):
                        self._share_server(entry)
        elif msg.method == METHOD_SESSION_UPDATE:
            update = msg.params.get("update")
            if isinstance(update, dict) and update.get("sessionUpdate") == "config_option_update":
                options = update.get("configOptions")
                if isinstance(options, list):
                    update["configOptions"] = self._shared_options(options)

    def _share_server(self, entry: dict[str, Any]) -> None:
        name = entry.get("name")
        if not isinstance(name, str):
            return
        lists = self._by_server.get(name)
        if lists is None:
            if len(self._by_server) >= MAX_SERVERS:
                return
            lists = self._by_server[name] = {}
        for field in _SERVER_LISTS:
            value = entry.get(field)
            if not isinstance(value, list) or not value:
                continue
            held = lists.get(field)
            if held is not None and _matches(held, value):
                entry[field] = held
            else:
                lists[field] = value

    def _shared_options(self, options: list[Any]) -> list[Any]:
        held = self._config_options
        if held is not None and _matches(held, options):
            return held
        self._config_options = options
        return options
