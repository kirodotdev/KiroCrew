"""A ``/mcp add`` or ``/mcp remove`` the person typed in chat becomes an ``mcp.server`` card.

kiro-cli's own ``/mcp add`` writes MCP files an agent's process cannot write (the
"agents ask before changing gateway-trusted files" entries in ``docs/decisions/``),
and KAS has no such command. So on every harness Crew reads the typed command
itself, in kiro-cli's own flag spelling, and raises the same card ``request_server``
raises: prefilled with the server's entry, applied only when the person presses it.
A command this cannot read, or one that names a place the card cannot write, gets
one reply pointing at the MCP Servers page instead.

Only words the person typed reach this. A messaging-channel turn gets the page
reply, in the transcript and in the linked Slack thread, because a card is
answered in the dashboard. Every other
``/mcp`` (the server list, ``/mcp auth``) keeps its existing path.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: The subcommands read here. Every other ``/mcp`` word keeps its existing path.
SUBCOMMANDS = ("add", "remove")
#: Where the MCP Servers page lives in the dashboard.
MCP_PAGE_PATH = "/mcp"
#: The one reply for a command the card cannot carry.
PAGE_REPLY = f"Add or remove MCP servers on the [MCP Servers]({MCP_PAGE_PATH}) page."
#: The same reply for a messaging channel, where a dashboard path is no link.
CHANNEL_REPLY = "Add or remove MCP servers on the MCP Servers page in the Kiro Crew dashboard."
#: kiro-cli's ``--scope`` values, and the card ``target`` each maps to.
_SCOPE_TARGETS = {"default": "crew", "global": "kiro-cli"}
_VALUE_FLAGS = frozenset({"name", "scope", "command", "url", "args", "env", "agent", "timeout"})
_REPEATABLE = frozenset({"args", "env"})


class TypedMcpUnreadable(ValueError):
    """The command cannot become a card; ``str()`` says why, for the log."""


@dataclass
class TypedMcpRequest:
    """One typed ``/mcp add`` or ``/mcp remove``, as an ``mcp.server`` card's parameters."""

    action: str
    server: str
    entry: dict[str, Any] = field(default_factory=dict)
    target: str = "crew"

    def card_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"action": self.action, "server": self.server, "use_now": True}
        if self.action != "remove":
            params["entry"] = self.entry
            # The person made no claim about the chat's folder, and the folder
            # detector cannot tell a git or code tool by its name, so a typed add
            # stays per-chat: the card never offers sharing for it.
            params["uses_chat_folder"] = True
        if self.target != "crew":
            params["target"] = self.target
        return params


def is_typed_mcp_change(message: str) -> bool:
    """Whether *message* is ``/mcp add ...`` or ``/mcp remove ...``."""
    parts = message.strip().split(None, 2)
    return len(parts) >= 2 and parts[0] == "/mcp" and parts[1] in SUBCOMMANDS


def typed_mcp_origin(turn: Any, message: str, *, synthetic: bool) -> str | None:
    """``"dashboard"`` or ``"channel"`` when the person typed this ``/mcp`` change, else ``None``.

    Read before any harness is looked up, so the answer is the same on KAS, kiro-cli
    and every other backend. A wake, a synthetic prompt, a turn with an actor (an
    app or the gateway) or any other ``/mcp`` gets ``None`` and keeps its path.
    """
    if synthetic or getattr(turn, "_directive_self_wake", False):
        return None
    if getattr(turn, "_turn_actor", None) or not is_typed_mcp_change(message):
        return None
    if getattr(turn, "_directive_channel_origin", False):
        return "channel"
    return "dashboard" if getattr(turn, "_directive_user_origin", False) else None


def _split_list(value: str) -> list[str]:
    """kiro-cli's ``--args`` forms: a JSON array, or commas with ``\\,`` kept as a comma."""
    text = value.strip()
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError as exc:
            raise TypedMcpUnreadable("--args is not a JSON array") from exc
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise TypedMcpUnreadable("--args must be a JSON array of strings")
        return items
    return [part.replace("\\,", ",") for part in re.split(r"(?<!\\),", value)]


def _flags(tokens: list[str]) -> tuple[dict[str, list[str]], set[str]]:
    values: dict[str, list[str]] = {}
    switches: set[str] = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if not token.startswith("--"):
            raise TypedMcpUnreadable(f"unexpected word {token!r}")
        name, eq, inline = token[2:].partition("=")
        if name == "disabled" and not eq:
            switches.add(name)
            i += 1
            continue
        if name not in _VALUE_FLAGS:
            raise TypedMcpUnreadable(f"unknown flag --{name}")
        if eq:
            value = inline
        else:
            if i + 1 >= len(tokens):
                raise TypedMcpUnreadable(f"--{name} needs a value")
            value = tokens[i + 1]
            i += 1
        if name in values and name not in _REPEATABLE:
            raise TypedMcpUnreadable(f"--{name} is given twice")
        values.setdefault(name, []).append(value)
        i += 1
    return values, switches


def parse_typed_mcp(message: str) -> TypedMcpRequest:
    """Read a typed ``/mcp add|remove`` in kiro-cli's flag spelling. Raises :class:`TypedMcpUnreadable`."""
    try:
        tokens = shlex.split(message.strip())
    except ValueError as exc:
        raise TypedMcpUnreadable("unbalanced quotes") from exc
    if len(tokens) < 2 or tokens[0] != "/mcp" or tokens[1] not in SUBCOMMANDS:
        raise TypedMcpUnreadable("not /mcp add or /mcp remove")
    action = tokens[1]
    values, switches = _flags(tokens[2:])
    one = {k: v[0] for k, v in values.items() if k not in _REPEATABLE}
    name = one.get("name", "").strip()
    if not name:
        raise TypedMcpUnreadable("--name is required")
    if "agent" in one:
        raise TypedMcpUnreadable("--agent is not a place the card writes")
    scope = one.get("scope", "default")
    if scope not in _SCOPE_TARGETS:
        raise TypedMcpUnreadable(f"--scope {scope} is not a place the card writes")
    target = _SCOPE_TARGETS[scope]
    if action == "remove":
        if set(values) - {"name"} or switches:
            # The card's remove takes the server out of every file the MCP page
            # lists, so a remove limited to one --scope goes to the page instead.
            raise TypedMcpUnreadable("/mcp remove takes only --name")
        return TypedMcpRequest(action="remove", server=name)
    if switches or "timeout" in one:
        raise TypedMcpUnreadable("--disabled and --timeout are set on the MCP Servers page")
    command, url = one.get("command"), one.get("url")
    if bool(command) == bool(url):
        raise TypedMcpUnreadable("give exactly one of --command or --url")
    entry: dict[str, Any]
    if url:
        if "args" in values or "env" in values:
            raise TypedMcpUnreadable("--args and --env are for a --command server")
        entry = {"url": url}
    else:
        entry = {"command": command}
        args = [a for v in values.get("args", []) for a in _split_list(v)]
        if args:
            entry["args"] = args
        env: dict[str, str] = {}
        for pair in (p for v in values.get("env", []) for p in _split_list(v)):
            key, eq, val = pair.partition("=")
            if not eq or not key.strip():
                raise TypedMcpUnreadable("--env takes KEY=VALUE")
            env[key.strip()] = val
        if env:
            entry["env"] = env
    return TypedMcpRequest(action="add", server=name, entry=entry, target=target)


async def answer_typed_mcp(
    state: Any,
    slot: Any,
    session_key: str,
    message: str,
    *,
    channel: bool,
    reply_with: Callable[[Any, str], None],
    reply_to_channel: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Raise the card for a typed ``/mcp add|remove``, or reply with the page link.

    *reply_with* writes a command reply row (the chat runner's
    ``_append_command_reply``), passed in because the runner's owners are its own.
    On a channel turn the turn ends before the reply mirror runs, so
    *reply_to_channel* posts :data:`CHANNEL_REPLY` where the person typed it.

    Returns the outcome it records: ``card``, ``page`` (unreadable or a channel turn)
    or ``refused`` (the card engine refused it; the reply names the page too).
    """
    from kiro_crew.change_card_catalog import CardCatalogError
    from kiro_crew.change_card_mcp_server import KIND_MCP_SERVER
    from kiro_crew.dashboard.change_cards import CardError
    from kiro_crew.dashboard.handlers.change_cards_propose import propose_in_slot
    from kiro_crew.sel import sel

    outcome, error = "page", ""
    reply = PAGE_REPLY
    try:
        request = parse_typed_mcp(message)
    except TypedMcpUnreadable as exc:
        request, error = None, str(exc)
    if request is not None and not channel:
        try:
            await propose_in_slot(
                state,
                slot.key,
                session_key,
                kind=KIND_MCP_SERVER,
                params=request.card_params(),
                reason="You typed /mcp " + request.action + " in this chat.",
            )
        except (CardError, CardCatalogError) as exc:
            outcome, error = "refused", getattr(exc, "message", str(exc))
            reply = f"{error[:1].upper()}{error[1:]}. {PAGE_REPLY}"
        else:
            outcome, reply = "card", ""
    try:
        sel().log_tool_invocation(
            session_key=session_key,
            agent=getattr(slot, "agent", None) or "kirocrew",
            source="dashboard",
            tool_name="/mcp " + (request.action if request else "change"),
            tool_kind="slash_command",
            outcome=outcome,
            metadata={"slot": slot.key, **({"error": error[:200]} if error else {})},
        )
    except Exception:  # noqa: BLE001 - an audit row never changes the answer
        pass
    if reply:
        reply_with(slot, reply)
    state.push_slots_update()
    if channel and reply_to_channel is not None:
        await reply_to_channel(CHANNEL_REPLY)
    return outcome
