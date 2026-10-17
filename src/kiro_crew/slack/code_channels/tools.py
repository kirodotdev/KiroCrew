"""The bound agent's code-channel tools, run in the gateway and pinned to its channel.

The MCP side (:mod:`kiro_crew.mcp_tools.code_channels`) only forwards a call to
``POST /api/slack/code-channel/tool`` with the caller's strictly resolved session
key; this module decides everything. The channel a tool acts on is NEVER an
argument: it is the channel bound to the dashboard slot the verified session key
belongs to (:func:`resolve_caller`). An argument that names a channel is refused
outright, so a model cannot even ask.

Every call is refused, and SEL-audited as denied, unless all of these hold:

* the code-channel gate is open (:func:`.binding.code_channels_on`);
* the caller's session resolves to a dashboard slot that a code channel is bound
  to, and the channel's Slack link still resolves to that same slot (a channel
  whose session was closed and replaced belongs to the replacement);
* the channel is not archived, and Slack's Stop has not silenced a turn of it.

Tools
-----

``code_channel_set_view``
    an ``html`` or ``block_kit`` tab. The Code tab's diff is the workspace's,
    and the plan tab comes with the plan canvas. HTML is self-contained (no
    ``csp`` resource domains are ever sent); Block Kit is display-only (no
    interactive elements, no remote media).
``code_channel_publish_plan_canvas``
    create the channel canvas, or replace its content; a create Slack refuses
    with ``channel_canvas_already_exists`` is never retried, and a canvas this
    gateway did not create is never written.
``code_channel_set_context_bar``
    the agent's own items, shown after the workspace's repo / branch / PR / CI
    items (five in all); informational and link items only.
``code_channel_rename``
    ``agents.sessions.rename`` (the channel and the session title).
``code_channel_set_status``
    ``processing`` / ``suspended`` / ``active``; ``suspended`` outlasts the turn.
``code_channel_wrap_up``
    post the wrap-up summary with its Archive button (archiving stays a person's
    click).

There is deliberately no tool that creates a channel, archives one, or names any
channel but the caller's own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from kiro_crew.slack.code_channels import surface
from kiro_crew.slack.code_channels.binding import code_channels_on, live_config
from kiro_crew.slack.code_channels.models import (
    MAX_TITLE_CHARS,
    CodeChannelApiError,
    CodeChannelRequestError,
)
from kiro_crew.slack.code_channels.store import Binding
from kiro_crew.slack.code_channels.surface_client import (
    CANVAS_ALREADY_EXISTS,
    MAX_CONTENT_BYTES,
)
from kiro_crew.slack.code_channels.surface_store import get_async as surface_get
from kiro_crew.slack.code_channels.surface_store import update_async as surface_update

logger = logging.getLogger(__name__)

SET_VIEW = "code_channel_set_view"
PUBLISH_PLAN_CANVAS = "code_channel_publish_plan_canvas"
SET_CONTEXT_BAR = "code_channel_set_context_bar"
RENAME = "code_channel_rename"
SET_STATUS = "code_channel_set_status"
WRAP_UP = "code_channel_wrap_up"
TOOL_NAMES = frozenset(
    {SET_VIEW, PUBLISH_PLAN_CANVAS, SET_CONTEXT_BAR, RENAME, SET_STATUS, WRAP_UP}
)

#: The plan canvas's view key; the agent's own views may not take it.
PLAN_VIEW_KEY = "plan"
#: Arguments that would name a channel. The tools take none.
_CHANNEL_ARGS = frozenset(
    {"channel", "channel_id", "channel_name", "target", "team_id", "conversation"}
)
_VIEW_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_BAR_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_VIEW_NAME = 80
_MAX_BLOCKS = 50
_INTERACTIVE_BLOCKS = frozenset({"actions", "input"})
#: Block types a display-only view may hold (an allowlist: newer containers can
#: carry buttons under keys a denylist would not know).
_DISPLAY_BLOCKS = frozenset({"section", "header", "divider", "context", "rich_text", "markdown"})
_MAX_LABEL = 128
_MAX_URL = 2048
_STATUSES = frozenset({"processing", "suspended", "active"})
_ICONS = frozenset(
    {
        "branch",
        "folder",
        "hierarchy",
        "life-ring",
        "link",
        "globe",
        "terminal",
        "code",
        "search",
        "lock",
    }
)

OFF_TEXT = "Error: Slack code channels are off on this Kiro Crew, so this tool does nothing."
NOT_BOUND_TEXT = (
    "Error: this session is not bound to a Slack code channel. The code-channel tools act "
    "only on the channel the calling session runs in, so there is nothing to act on here."
)
ARCHIVED_TEXT = "Error: this session's code channel is archived."
STOPPED_TEXT = (
    "Error: someone pressed Stop in this session's code channel; leave the channel alone "
    "until the next message."
)
GOVERNANCE_TEXT = "Error: this Kiro Crew's channels policy does not allow writing to Slack."
NO_SLACK_TEXT = "Error: Slack is not connected right now; try again in a moment."


class ArgumentError(ValueError):
    """The call's arguments were refused before anything reached Slack."""


class ToolRefusal(Exception):
    """The tool ran and could not do what was asked; *text* tells the agent why."""

    def __init__(self, text: str, code: str) -> None:
        super().__init__(text)
        self.text = text
        self.code = code


@dataclass(frozen=True)
class ToolOutcome:
    ok: bool
    text: str
    code: str = ""


@dataclass(frozen=True)
class Call:
    """One tool call, resolved: the caller's binding and a live client."""

    binding: Binding
    client: Any
    caller: str


# ── Caller resolution ─────────────────────────────────────────────────────


async def resolve_caller(state: Any, session_key: str) -> tuple[Binding | None, str]:
    """``(binding, slot_key)`` for the verified *session_key* (binding None when unbound)."""
    slot_key = surface.caller_slot_key(state, session_key)
    return await surface.binding_for_slot(state, slot_key), slot_key


async def run_tool(state: Any, session_key: str, tool: str, args: Any) -> ToolOutcome:
    """Run *tool* for the session *session_key* names (already transport-verified)."""
    operation = "slack.code_channel.tool"
    if tool not in TOOL_NAMES:
        surface.audit(session_key, operation, "denied", f"tool={tool[:64]}", "unknown tool")
        return ToolOutcome(
            False, f"Error: unknown code-channel tool {tool[:64]!r}.", "unknown_tool"
        )
    if not code_channels_on(live_config()):
        surface.audit(session_key, operation, "denied", f"tool={tool}", "feature off")
        return ToolOutcome(False, OFF_TEXT, "feature_off")
    binding, slot_key = await resolve_caller(state, session_key)
    if binding is None:
        surface.audit(
            session_key, operation, "denied", f"tool={tool} slot={slot_key or '-'}", "not bound"
        )
        return ToolOutcome(False, NOT_BOUND_TEXT, "not_bound")
    resources = f"tool={tool} channel={binding.channel_id} slot={slot_key}"
    if not isinstance(args, dict):
        args = {}
    named = sorted(_CHANNEL_ARGS & set(args))
    if named:
        surface.audit(session_key, operation, "denied", resources, f"channel argument {named[0]}")
        return ToolOutcome(
            False,
            f"Error: {tool} takes no {named[0]!r}: it always acts on this session's own code "
            "channel, and no other channel can be named.",
            "channel_argument",
        )
    if await surface.is_archived(binding):
        surface.audit(session_key, operation, "denied", resources, "archived")
        return ToolOutcome(False, ARCHIVED_TEXT, "archived")
    if surface.channel_stopped(binding.channel_id):
        surface.audit(session_key, operation, "denied", resources, "stopped")
        return ToolOutcome(False, STOPPED_TEXT, "stopped")
    if not await surface.outbound_permitted():
        surface.audit(session_key, operation, "denied", resources, "channels governance policy")
        return ToolOutcome(False, GOVERNANCE_TEXT, "channels_policy")
    client = surface.client()
    if client is None:
        surface.audit(session_key, operation, "error", resources, "no Slack client")
        return ToolOutcome(False, NO_SLACK_TEXT, "no_client")
    call = Call(binding=binding, client=client, caller=session_key)
    try:
        text = await _HANDLERS[tool](call, args)
    except (ArgumentError, CodeChannelRequestError) as exc:
        surface.audit(
            session_key, operation, "denied", resources, f"invalid arguments: {str(exc)[:160]}"
        )
        return ToolOutcome(False, f"Error: {exc}", "invalid_arguments")
    except CodeChannelApiError as exc:
        surface.audit(session_key, operation, "error", resources, exc.code)
        return ToolOutcome(False, _api_error_text(tool, exc), exc.code)
    except ToolRefusal as exc:
        surface.audit(session_key, operation, "error", resources, exc.code)
        return ToolOutcome(False, exc.text, exc.code)
    except Exception:
        logger.exception("code channel tool %s failed", tool)
        surface.audit(session_key, operation, "error", resources, "internal error")
        return ToolOutcome(
            False, "Error: the tool failed unexpectedly; part of it may have applied.", "internal"
        )
    surface.audit(session_key, operation, "allowed", resources)
    return ToolOutcome(True, text)


def _api_error_text(tool: str, exc: CodeChannelApiError) -> str:
    if exc.unavailable:
        return (
            f"Error: Slack answered {exc.code}: Slack code channels are not available to "
            "this app here, so the channel was left as it was."
        )
    if exc.maybe_applied:
        return f"Error: the call to Slack did not complete ({exc.code}); it may have applied."
    return f"Error: Slack refused {tool} ({exc.code})."


# ── Argument helpers ──────────────────────────────────────────────────────


def _only(args: dict[str, Any], allowed: set[str], tool: str) -> None:
    extra = sorted(set(args) - allowed)
    if extra:
        raise ArgumentError(f"{tool} does not take {', '.join(repr(k) for k in extra)}")


def _text(args: dict[str, Any], name: str, *, required: bool = False, max_len: int = 0) -> str:
    value = args.get(name)
    if value is None or value == "":
        if required:
            raise ArgumentError(f"{name} is required")
        return ""
    if not isinstance(value, str):
        raise ArgumentError(f"{name} must be a string")
    value = value.strip()
    if required and not value:
        raise ArgumentError(f"{name} is required")
    if max_len and len(value) > max_len:
        raise ArgumentError(f"{name} is longer than {max_len} characters")
    return value


def _redact(text: str) -> str:
    from kiro_crew.slack.code_channels.wrapup import _redact as redact

    return redact(text)


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return _redact(value)
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    if isinstance(value, dict):
        return {_redact(str(k)): _redact_tree(v) for k, v in value.items()}
    return value


def _check_size(text: str, what: str) -> None:
    if len(text.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ArgumentError(f"{what} exceeds {MAX_CONTENT_BYTES} bytes")


def _interactive(node: Any) -> bool:
    """Whether a Block Kit tree carries an element a person could press or fill in."""
    if isinstance(node, dict):
        if node.get("type") in _INTERACTIVE_BLOCKS or "action_id" in node or "accessory" in node:
            return True
        return any(_interactive(v) for v in node.values())
    if isinstance(node, list):
        return any(_interactive(v) for v in node)
    return False


def _blocks(args: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    from kiro_crew.slack.client import blocks_request_remote_media

    raw = args.get("blocks")
    if not isinstance(raw, list) or not raw:
        raise ArgumentError("a block_kit view needs a non-empty blocks list")
    if len(raw) > _MAX_BLOCKS or not all(isinstance(b, dict) for b in raw):
        raise ArgumentError(f"blocks must be at most {_MAX_BLOCKS} Block Kit objects")
    unknown = sorted({str(b.get("type")) for b in raw} - _DISPLAY_BLOCKS)
    if unknown:
        raise ArgumentError(
            f"a block_kit view is display-only here; block types allowed: "
            f"{', '.join(sorted(_DISPLAY_BLOCKS))} (got {', '.join(unknown)})"
        )
    if _interactive(raw):
        raise ArgumentError(
            "a block_kit view is display-only here: no actions/input blocks, accessories "
            "or action_id elements"
        )
    if blocks_request_remote_media(raw):
        raise ArgumentError("blocks may not load remote images or video")
    _check_size(json.dumps(raw), "blocks")
    return tuple(_redact_tree(raw))


# ── Tools ─────────────────────────────────────────────────────────────────


async def _set_view(call: Call, args: dict[str, Any]) -> str:
    _only(
        args,
        {"type", "view_key", "name", "content", "blocks"},
        SET_VIEW,
    )
    kind = _text(args, "type") or "html"
    if kind in ("diff", "pull_request"):
        raise ArgumentError(
            f"{kind} views are published from the worktree automatically; use html, block_kit or canvas"
        )
    if kind == "canvas":
        raise ArgumentError("the plan canvas tab comes with code_channel_publish_plan_canvas")
    if kind not in ("html", "block_kit"):
        raise ArgumentError("type must be html or block_kit")
    name = _redact(_text(args, "name", max_len=_MAX_VIEW_NAME))
    view_key = _text(args, "view_key", required=True)
    if not _VIEW_KEY_RE.match(view_key):
        raise ArgumentError("view_key must be 1-128 letters, digits, '.', '_', '/' or '-'")
    if _redact(view_key) != view_key:
        raise ArgumentError(
            "that view_key carries data redaction would remove; choose a plain name"
        )
    if view_key == PLAN_VIEW_KEY:
        raise ArgumentError(f"view_key {PLAN_VIEW_KEY!r} is the plan canvas's; choose another")
    if kind == "html":
        content = args.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ArgumentError("an html view needs content: a full, self-contained HTML document")
        _check_size(content, "content")
        view_id = await call.client.set_view(
            call.binding.channel_id,
            type="html",
            view_key=view_key,
            content=_redact(content),
            name=name,
        )
    else:
        view_id = await call.client.set_view(
            call.binding.channel_id,
            type="block_kit",
            view_key=view_key,
            blocks=_blocks(args),
            name=name,
        )
    label = name or view_key
    return f"Published the {kind} view {label!r} in this channel (view {view_id})."


async def _publish_plan_canvas(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"markdown", "title"}, PUBLISH_PLAN_CANVAS)
    markdown = args.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip():
        raise ArgumentError("markdown is required")
    _check_size(markdown, "markdown")
    markdown = _redact(markdown)
    title = _redact(_text(args, "title", max_len=MAX_TITLE_CHARS))
    channel = call.binding.channel_id
    record = await surface_get(channel)
    canvas_id = record.plan_canvas_id if record is not None else ""
    created = False
    if not canvas_id:
        try:
            canvas_id = await call.client.create_canvas(channel, markdown, title=title)
            created = True
        except CodeChannelApiError as exc:
            if exc.code != CANVAS_ALREADY_EXISTS:
                raise
            # Never retried, and never adopted: a canvas this gateway did not
            # create may be a shared document, and writing it would edit it
            # everywhere it appears.
            raise ToolRefusal(
                "Error: this channel already has a canvas that Kiro Crew did not create, "
                "so it was left alone. Share the plan as an html view instead.",
                CANVAS_ALREADY_EXISTS,
            ) from None
        if not canvas_id:
            raise CodeChannelApiError("conversations.canvases.create", "missing_canvas_id")
        await surface_update(channel, plan_canvas_id=canvas_id)
    if not created:
        await call.client.set_canvas_content(channel, canvas_id, markdown)
    view_note = ""
    record = await surface_get(channel)
    if record is None or not record.plan_view_id:
        try:
            view_id = await call.client.set_view(
                channel,
                type="canvas",
                view_key=PLAN_VIEW_KEY,
                canvas_id=canvas_id,
                access_level="comment",
                name="Plan",
            )
            await surface_update(channel, plan_view_id=view_id)
            view_note = " It is the Plan tab; people can comment, only you edit it."
        except CodeChannelApiError as exc:
            logger.info("code channel %s: plan tab not attached: %s", channel, exc.code)
    verb = "Created" if created else "Updated"
    return f"{verb} this channel's plan canvas.{view_note}"


def _bar_items(args: dict[str, Any]) -> list[dict[str, str]]:
    from kiro_crew.slack.code_channels.agent_bar import MAX_ITEMS, RESERVED_KEYS

    raw = args.get("items")
    if not isinstance(raw, list):
        raise ArgumentError("items must be a list (an empty list clears your items)")
    if len(raw) > MAX_ITEMS:
        raise ArgumentError(f"at most {MAX_ITEMS} items")
    items: list[dict[str, str]] = []
    keys: set[str] = set()
    for raw_item in raw:
        if not isinstance(raw_item, dict):
            raise ArgumentError("each item is an object with key, label, icon and url")
        extra = sorted(set(raw_item) - {"key", "label", "icon", "url"})
        if "item_type" in extra:
            raise ArgumentError(
                "action items are not supported: Slack does not deliver their clicks to "
                "Kiro Crew. Use a url item instead"
            )
        if extra:
            raise ArgumentError(f"context bar items do not take {', '.join(extra)}")
        key = _text(raw_item, "key", required=True)
        if not _BAR_KEY_RE.match(key):
            raise ArgumentError("a key is 1-64 lowercase letters, digits, '_' or '-'")
        if _redact(key) != key:
            raise ArgumentError("that key carries data redaction would remove; choose a plain name")
        if key in RESERVED_KEYS:
            raise ArgumentError(f"key {key!r} is the workspace's own item; choose another")
        if key in keys:
            raise ArgumentError(f"key {key!r} appears twice")
        keys.add(key)
        item = {
            "key": key,
            "label": _redact(_text(raw_item, "label", required=True, max_len=_MAX_LABEL)),
        }
        icon = _text(raw_item, "icon")
        if icon:
            if icon not in _ICONS:
                raise ArgumentError(f"icon must be one of {', '.join(sorted(_ICONS))}")
            item["icon"] = icon
        url = _text(raw_item, "url", max_len=_MAX_URL)
        if url:
            if not url.startswith("https://"):
                raise ArgumentError("a url must start with https://")
            if _redact(url) != url:
                raise ArgumentError(
                    "that url carries data redaction would remove; link a plain page"
                )
            item["url"] = url
        items.append(item)
    return items


async def _set_context_bar(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"items"}, SET_CONTEXT_BAR)
    from kiro_crew.slack.code_channels import agent_bar
    from kiro_crew.slack.code_channels.workspace import core as workspace

    items = _bar_items(args)
    channel = call.binding.channel_id
    previous = await surface_get(channel)
    await surface_update(channel, context_bar=items)
    service = getattr(workspace, "_SERVICE", None)
    result = await service.republish_bar(call.binding) if service is not None else None
    if result is None:
        # No workspace record to publish from: the bar is the agent's items alone.
        merged, _ = agent_bar.merge([], items)
        try:
            await call.client.set_context_bar(channel, merged)
        except CodeChannelApiError:
            await surface_update(channel, context_bar=previous.context_bar if previous else ())
            raise
    elif result:
        await surface_update(channel, context_bar=previous.context_bar if previous else ())
        raise CodeChannelApiError("agents.conversations.setProperties", result)
    base_keys = await asyncio.to_thread(_workspace_keys, call.binding)
    _, dropped = agent_bar.merge([{"key": k} for k in base_keys], items)
    shown = [i["key"] for i in items if i["key"] not in dropped]
    if not items:
        return "Cleared your context-bar items; the workspace's items stay."
    note = (
        f" Not shown (the bar holds five, the workspace's first): {', '.join(dropped)}."
        if dropped
        else ""
    )
    return f"Context bar updated: {', '.join(shown) or 'none of your items'} shown.{note}"


def _workspace_keys(binding: Binding) -> list[str]:
    """The keys the workspace's own bar items take (best effort, for the reply)."""
    try:
        from kiro_crew.slack.code_channels.workspace.store import WorkspaceStore

        record = WorkspaceStore().get(binding.team_id, binding.channel_id)
    except Exception:
        return []
    if record is None:
        return []
    keys = ["repo" if record.repo_root else "folder"]
    if record.branch:
        keys.append("branch")
    if record.pr_number:
        keys.append("pr")
        if record.pr_ci and record.pr_state in ("open", "draft"):
            keys.append("ci")
    return keys


async def _rename(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"title"}, RENAME)
    title = _redact(_text(args, "title", required=True, max_len=MAX_TITLE_CHARS))
    recorded = await call.client.rename(call.binding.channel_id, title)
    return f"Renamed this code channel and session to {recorded!r}."


async def _set_status(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"status"}, SET_STATUS)
    from kiro_crew.slack.code_channels import status as channel_status

    value = _text(args, "status", required=True)
    if value not in _STATUSES:
        raise ArgumentError("status must be processing, suspended or active")
    await call.client.set_status(call.binding.channel_id, value)
    channel_status.hold_after_turn(call.binding.channel_id, value if value == "suspended" else None)
    if value == "suspended":
        return "Status set to suspended: the channel shows you are waiting on its people, and stays so after this turn."
    return f"Status set to {value}."


async def _wrap_up(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"summary"}, WRAP_UP)
    from kiro_crew.slack.code_channels.wrapup import MAX_SUMMARY_CHARS, post_wrap_up

    summary = _text(args, "summary", required=True, max_len=MAX_SUMMARY_CHARS)
    ts = await post_wrap_up(call.binding, summary, caller=call.caller)
    if not ts:
        raise ToolRefusal(
            "Error: the wrap-up could not be posted; try again in a moment.", "post_failed"
        )
    return (
        "Posted the wrap-up with an Archive button. The people in the channel archive it; "
        "do not post anything after it unless they ask."
    )


_HANDLERS: dict[str, Callable[[Call, dict[str, Any]], Awaitable[str]]] = {
    SET_VIEW: _set_view,
    PUBLISH_PLAN_CANVAS: _publish_plan_canvas,
    SET_CONTEXT_BAR: _set_context_bar,
    RENAME: _rename,
    SET_STATUS: _set_status,
    WRAP_UP: _wrap_up,
}
