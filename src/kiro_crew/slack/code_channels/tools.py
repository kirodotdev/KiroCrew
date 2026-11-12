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
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Iterable

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
CHANNEL_REFUSED_TEXT = (
    "Error: this Kiro Crew's channel policy does not allow this code channel, so "
    "nothing was posted there."
)
CHANNEL_OFF_TEXT = (
    "Error: the bot is off (or in review mode) in this code channel or the channel it "
    "came from, so nothing was posted there."
)


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
    """Run *tool* for the session *session_key* names (already transport-verified).

    Admitted at the stop fence first, before caller resolution: a Stop that
    lands anywhere in the call (resolution, redaction, a canvas read) leaves it
    sending nothing more (``SurfaceClient._call``).
    """
    token = surface.admit()
    try:
        return await _run_tool(state, session_key, tool, args)
    finally:
        surface.release(token)


async def _run_tool(state: Any, session_key: str, tool: str, args: Any) -> ToolOutcome:
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
    if not await surface.caller_slack_permitted(session_key):
        surface.audit(
            session_key, operation, "denied", resources, "calling session's channels policy"
        )
        return ToolOutcome(False, GOVERNANCE_TEXT, "channels_policy")
    if not surface.channel_admits(live_config(), binding):
        surface.audit(
            session_key, operation, "denied", resources, "channel or origin does not admit the bot"
        )
        return ToolOutcome(False, CHANNEL_OFF_TEXT, "channel_off")
    if surface.channel_policy_refusal(binding.channel_id, operation=operation, caller=session_key):
        return ToolOutcome(False, CHANNEL_REFUSED_TEXT, "channel_refused")
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
    from kiro_crew.slack.code_channels.surface_client import STOPPED

    if exc.code == STOPPED:
        return STOPPED_TEXT
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


def _redact_payload(text: str) -> str:
    from kiro_crew.slack.code_channels.wrapup import _redact_payload as redact

    return redact(text)


#: Block Kit keys whose value is a link destination, not text Slack parses.
_URL_KEYS = frozenset({"url"})


def _redact_tree(value: Any, key: str = "") -> Any:
    if isinstance(value, str):
        # A destination gets every redaction pass but no mention defang, which
        # would only break it (``/@scope/`` package links).
        return _redact_payload(value) if key in _URL_KEYS else _redact(value)
    if isinstance(value, list):
        return [_redact_tree(v, key) for v in value]
    if isinstance(value, dict):
        return {_redact(str(k)): _redact_tree(v, str(k)) for k, v in value.items()}
    return value


#: How many nested levels of character references an HTML view may carry; one
#: still changing after this many is refused rather than published unscreened.
_MAX_DECODE_ROUNDS = 4
#: A ``srcdoc`` attribute: whitespace, ``/`` or a closing quote separates
#: attributes, and attribute names are never character-reference decoded. Matched
#: anywhere (not only inside a tag, which a ``>`` in a quoted value would cut
#: short), so text that literally reads `` srcdoc=`` is refused too.
_NESTED_DOCUMENT = re.compile(r"(?:^|[\s/\"'])srcdoc\s*=", re.IGNORECASE)

#: Block Kit keys Slack shows as text: a text object's ``text``, a date element's
#: ``format`` and ``fallback`` (a link with no ``text`` shows its ``url``, below).
_SHOWN_KEYS = frozenset({"text", "format", "fallback"})


def _shown_texts(value: Any) -> list[str]:
    """Every Block Kit display string, in reading order."""
    if isinstance(value, list):
        return [t for v in value for t in _shown_texts(v)]
    if isinstance(value, dict):
        out: list[str] = []
        if (
            value.get("type") == "link"
            and "text" not in value
            and isinstance(value.get("url"), str)
        ):
            out.append(value["url"])
        for k, v in value.items():
            out.extend([v] if k in _SHOWN_KEYS and isinstance(v, str) else _shown_texts(v))
        return out
    return []


#: Elements that open a nested document or plugin, which no screen here reads,
#: and foreign content (SVG, MathML), which a browser parses by other rules
#: (character references decoded inside ``<style>``, raw-text elements changed).
_REFUSED_ELEMENTS = frozenset(
    {
        "iframe", "frame", "frameset", "object", "embed", "portal", "svg", "math",
        # an external stylesheet (or a rebased URL) the CSS screen never reads
        "link", "base",
        # parsed as raw text when scripting is on, as markup when it is off
        "noscript",
    }
)  # fmt: skip
#: Attributes that hold a URL a browser loads or navigates to.
_URL_ATTRS = frozenset(
    {"src", "href", "srcset", "imagesrcset", "poster", "action", "formaction", "data",
     "background", "cite", "longdesc", "ping", "xlink:href", "manifest", "content"}
)  # fmt: skip
#: Attributes holding a comma-separated list of URL candidates.
_URL_LIST_ATTRS = frozenset({"srcset", "imagesrcset"})
#: Raw-text elements: a browser ignores a self-closing slash on these and reads
#: what follows as their text, where ``HTMLParser`` closes them at once.
_RAW_TEXT_ELEMENTS = frozenset(
    {"style", "script", "textarea", "title", "xmp", "noscript", "noembed", "noframes", "plaintext"}
)
#: Attributes a browser shows as text on the page or in a tooltip.
_SHOWN_ATTRS = frozenset({"alt", "value", "placeholder", "label", "title"})
#: What a browser drops anywhere in a URL (tab, LF, CR), and before it.
_URL_NOISE = re.compile(r"[\t\n\r]")
_URL_LEAD = re.compile(r"^[\x00-\x20]+")
#: A marked section (``<![CDATA[``) in HTML is a bogus comment a browser ends at
#: the first ``>``; ``HTMLParser`` would run it to ``]]>``.
_MARKED_SECTION = re.compile(r"<!\[[^>]*(?:>|\Z)")
_CSS_ESCAPE = re.compile(r"\\([0-9a-fA-F]{1,6})[ \t\n\r\f]?|\\(.)", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.S)
#: CSS that can render author text or load another resource. With no string
#: token, ``attr()``, ``@import`` or ``@counter-style``, no property (``content``,
#: ``quotes``, ``list-style-type``, ...) has author text to show.
_CSS_AUTHOR_TEXT = re.compile(
    r"[\"']|(?<![-\w])attr\s*\(|@import|@counter-style|@font-face", re.IGNORECASE
)
_CSS_URL = re.compile(r"(?<![-\w])(?:url|src)\s*\(([^)]*)\)?", re.IGNORECASE)


def _css_decoded(css: str, *, keep_comments: bool = False) -> str:
    """*css* as a CSS parser reads it: escapes decoded, comments removed.

    Character references are not decoded: an HTML ``<style>`` never decodes them
    (foreign content, which does, is refused), and a ``style=`` value arrives
    from ``HTMLParser`` already decoded."""

    def one(m: re.Match[str]) -> str:
        if m.group(1):
            point = int(m.group(1), 16)
            return chr(point) if 0 < point <= 0x10FFFF else "\ufffd"
        return m.group(2)

    decoded = _CSS_ESCAPE.sub(one, css)
    return decoded if keep_comments else _CSS_COMMENT.sub(" ", decoded)


def _css_refusal(css: str) -> str:
    """Why *css* could show author text or load something (``""`` when it can't)."""
    decoded = _css_decoded(css)
    if _CSS_AUTHOR_TEXT.search(decoded):
        return (
            "an html view's CSS may not hold strings, attr(), @import, @counter-style "
            "or @font-face"
        )
    for m in _CSS_URL.finditer(decoded):
        if not _URL_NOISE.sub("", m.group(1)).strip().lower().startswith("https:"):
            return "an html view's CSS may load only https: URLs"
    return ""


def _is_data_url(key: str, value: str) -> bool:
    """Whether URL attribute *key* holds a ``data:`` URL (any ``srcset`` candidate)."""
    if key not in _URL_ATTRS:
        return False
    clean = _URL_NOISE.sub("", value).lower()
    parts = clean.split(",") if key in _URL_LIST_ATTRS else [clean]
    # A meta ``content`` (``0;url=data:...``) names its URL after ``url=``.
    if key == "content":
        parts = [clean.partition("url=")[2]] if "url=" in clean else []
    # A URL parser drops every leading C0 control or space.
    return any(_URL_LEAD.sub("", part).startswith("data:") for part in parts)


class _ViewAudit(HTMLParser):
    """One pass over a view: what it shows beyond text nodes, and why to refuse.

    ``HTMLParser`` hands attribute values back with character references
    decoded, as the browser reads them.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refusal = ""
        self.shown: list[str] = []  # text and shown attribute text, in order
        self.values: list[str] = []  # every attribute value
        self.styles: list[str] = []  # <style> text and style= values
        self._in_style = False
        # In HTML content ``<![CDATA[`` is a bogus comment ending at ``>``. Newer
        # CPython releases read it as CDATA unless told otherwise; older ones
        # reach parse_marked_section below.
        set_cdata = getattr(self, "_set_support_cdata", None)
        if callable(set_cdata):
            set_cdata(False)

    def _refuse(self, why: str) -> None:
        self.refusal = self.refusal or why

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _REFUSED_ELEMENTS:
            self._refuse(f"an html view may not use <{tag}> (embedded, foreign or linked content)")
        if tag == "style":
            self._in_style = True
        # Every value is screened, duplicates included (a browser keeps the first).
        for key, value in attrs:
            value = value or ""
            self.values.append(value)
            if key == "http-equiv" and value.strip().lower() == "refresh":
                self._refuse("an html view may not redirect (meta refresh)")
            if _is_data_url(key, value):
                self._refuse("an html view may not load a data: URL")
            if key == "style":
                self.styles.append(value)
            if key in _SHOWN_ATTRS and value:
                self.shown.append(value)

    def parse_comment(self, i: int, report: int = 1) -> int:
        # Comments end where a browser ends them (``<!-->`` is empty), the same
        # rule the shown-text projection uses, so both read one document.
        from kiro_crew.dashboard.card_lifecycle import _COMMENT

        end = _COMMENT.match(self.rawdata, i)
        assert end is not None  # ``\Z`` ends any comment the tokenizer opened
        return end.end()

    def parse_marked_section(self, i: int, report: int = 1) -> int:
        # Foreign content, the one place CDATA is real, is refused.
        end = _MARKED_SECTION.match(self.rawdata, i)
        return end.end() if end else i + 3

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _RAW_TEXT_ELEMENTS:
            self._refuse(f"an html view may not self-close <{tag}>")
        self.handle_starttag(tag, attrs)
        self._in_style = False  # a self-closed tag opens no style text

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False

    def handle_data(self, data: str) -> None:
        if self._in_style:
            self.styles.append(data)
        else:
            self.shown.append(data)


def _audit_view(content: str, redacted: str) -> None:
    """Refuse a view whose markup can show what the text screen never reads."""
    original, published = _ViewAudit(), _ViewAudit()
    original.feed(content)
    original.close()
    published.feed(redacted)
    published.close()
    # The original decides the structural refusals (redaction can only shorten
    # values); the published document is what the value screen reads.
    if original.refusal:
        raise ArgumentError(f"{original.refusal}; nothing was published")
    for css in original.styles:
        why = _css_refusal(css)
        if why:
            raise ArgumentError(f"{why}; nothing was published")
    hidden = (
        "a credential or sensitive value is hidden in an attribute or CSS; nothing was published"
    )
    # Comments are kept for this screen: not shown, but published all the same.
    for text in published.values + [
        _css_decoded(css, keep_comments=True) for css in published.styles
    ]:
        if _redact_payload(text) != text:
            raise ArgumentError(hidden)
    _refuse_hidden_split(zip(_joined(original.shown), _joined(published.shown)))


def _redact_html(content: str) -> str:
    """An HTML view's document, redacted and then screened as a browser shows it.

    The projection is the dashboard cards' own (both readings of a tag boundary:
    an inline one joins words, a block one separates them), so the two HTML
    sinks cannot read a page two ways.
    """
    from kiro_crew.dashboard.card_lifecycle import _html_texts

    # A nested document (``srcdoc``) is markup inside an attribute, which no
    # screen below reads as shown text; a display view never needs one.
    if _NESTED_DOCUMENT.search(content):
        raise ArgumentError("an html view may not nest a document (srcdoc); nothing was published")
    redacted = _redact_payload(content)
    _refuse_hidden_split(zip(_html_texts(content), _html_texts(redacted)))
    # Nested documents, data: URLs, CSS that shows text, and attribute text.
    _audit_view(content, redacted)
    # The browser decodes character references anywhere, attributes included,
    # which the raw redactor and the text projection never see. Screen every
    # decoded reading, as the dashboard cards do for one level, without
    # rewriting markup.
    # shortcut: script escapes are not modelled, and neither is layout that
    # reorders or hides text (bidi controls, <bdo>, display:none, row-reverse,
    # text-transform); upgrade to a tag/attribute/CSS-property allowlist if
    # Slack's HTML view runs scripts or the layout class must be closed.
    reading = redacted
    for _ in range(_MAX_DECODE_ROUNDS + 1):
        decoded = unescape(reading)
        if decoded == reading:
            return redacted
        if _redact_payload(decoded) != decoded:
            break
        reading = decoded
    raise ArgumentError(
        "a credential or sensitive value is hidden in character references; "
        "nothing was published"
    )


def _joined(texts: list[str]) -> tuple[str, str]:
    """Both readings of adjacent elements: run together, and word-separated."""
    return "".join(texts), " ".join(texts)


def _refuse_hidden_split(readings: Iterable[tuple[str, str]]) -> None:
    """Refuse a view whose shown text hides something redaction should remove.

    Each element is redacted on its own, but Slack shows adjacent elements (a
    rich-text run, inline HTML spans) as one run: a credential cut between them,
    or a label held apart from its value, passes every fragment and reads whole.
    ``readings`` pairs each reading of the original with the same reading of what
    would publish; the dashboard cards' own screen decides, with this surface's
    redactor. A hit refuses the view rather than guess which fragments to rewrite.
    """
    from kiro_crew.dashboard.card_lifecycle import readings_hide_secret

    if readings_hide_secret(readings, _redact_payload):
        raise ArgumentError(
            "a credential or sensitive value is split across display elements; "
            "nothing was published"
        )


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
    redacted = _redact_tree(raw)
    _refuse_hidden_split(zip(_joined(_shown_texts(raw)), _joined(_shown_texts(redacted))))
    return tuple(redacted)


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
    if _redact_payload(view_key) != view_key:
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
        # Up to MAX_CONTENT_BYTES through every display pass: off the event loop.
        content = await asyncio.to_thread(_redact_html, content)
        view_id = await call.client.set_view(
            call.binding.channel_id,
            type="html",
            view_key=view_key,
            content=content,
            name=name,
        )
    else:
        # Up to MAX_CONTENT_BYTES of JSON through every display pass: off the loop.
        blocks = await asyncio.to_thread(_blocks, args)
        view_id = await call.client.set_view(
            call.binding.channel_id,
            type="block_kit",
            view_key=view_key,
            blocks=blocks,
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
    # Up to MAX_CONTENT_BYTES through every display pass: off the event loop.
    safe_markdown: str = await asyncio.to_thread(_redact, markdown)
    title = _redact(_text(args, "title", max_len=MAX_TITLE_CHARS))
    channel = call.binding.channel_id
    # One publish per channel at a time: two concurrent first calls would both
    # create, and the loser's "already exists" would refuse the channel for good.
    async with _canvas_locks.setdefault(channel, asyncio.Lock()):
        return await _publish_canvas_locked(call, channel, safe_markdown, title)


#: Per-channel publish locks for the plan canvas.
_canvas_locks: dict[str, asyncio.Lock] = {}
#: Canvases created whose id could not be stored yet; written on the next call.
_unsaved_canvas: dict[str, str] = {}
#: Channels where a create of ours ran and its canvas id is not stored yet, and
#: when the earliest such create began (stored first as
#: ``canvas_create_pending_since``, so it outlasts a restart).
_maybe_created: dict[str, float] = {}
#: How far Slack's clock may run behind ours when a canvas's ``created`` is read.
_CLOCK_SKEW_SECS = 120.0


def forget_channel(channel_id: str) -> None:
    """Drop the in-memory canvas state of an archived channel (it takes no more calls)."""
    _canvas_locks.pop(channel_id, None)
    _unsaved_canvas.pop(channel_id, None)
    _maybe_created.pop(channel_id, None)
    _bar_locks.pop(channel_id, None)


def _commit(channel: str) -> None:
    """The commit point for a tool's writes: a stopped call raises, writing nothing."""
    if not surface.commit_if_not_stopped(channel):
        from kiro_crew.slack.code_channels.surface_client import STOPPED

        raise CodeChannelApiError("code_channel.commit", STOPPED)


def _may_have_landed(exc: BaseException | None) -> bool:
    return isinstance(exc, CodeChannelApiError) and exc.maybe_applied


async def _create_canvas(call: Call, channel: str, markdown: str, title: str, began: float) -> str:
    """Create the canvas; a cancel mid-call still keeps the id Slack hands back."""
    creating = asyncio.ensure_future(call.client.create_canvas(channel, markdown, title=title))
    try:
        return str(await asyncio.shield(creating) or "")
    except asyncio.CancelledError:
        while not creating.done():
            try:
                await asyncio.wait({creating})
            except asyncio.CancelledError:
                pass  # already cancelling: keep waiting for Slack's answer
        if not creating.cancelled() and creating.exception() is None and creating.result():
            _unsaved_canvas[channel] = str(creating.result())
        raise  # a lost answer is covered by the intent stored before the create


def _made_by_us(user: str, bot: str) -> bool:
    """Whether a canvas creator is this gateway's own bot (unknown identity: no)."""
    from kiro_crew.slack.enterprise import validated_self_bot_id, validated_self_user_id

    self_user, self_bot = validated_self_user_id(), validated_self_bot_id()
    return bool((self_user and user == self_user) or (self_bot and bot == self_bot))


async def _adopt_lost_create(call: Call, channel: str, began: float) -> str:
    """The canvas a lost create of ours made ("" unless that is certain).

    The channel's canvas is adopted only when ``files.info`` says this gateway's
    bot made it, no earlier than the lost create began: a canvas anyone else
    made, or one older than that create, is never written.
    """
    try:
        canvas_id = await call.client.channel_canvas_id(channel)
        if not canvas_id:
            return ""
        user, bot, created = await call.client.canvas_origin(canvas_id, channel_id=channel)
    except (CodeChannelApiError, CodeChannelRequestError) as exc:
        logger.info("code channel %s: channel canvas not verified: %s", channel, exc)
        return ""
    if not _made_by_us(user, bot):
        # Named, so a creator Slack reports in a form we don't recognise shows here.
        logger.info(
            "code channel %s: channel canvas %s is not ours (user=%r bot_id=%r)",
            channel,
            canvas_id,
            user[:32],
            bot[:32],
        )
        return ""
    if not began or not created or created < began - _CLOCK_SKEW_SECS:
        logger.info("code channel %s: canvas %s predates the lost create", channel, canvas_id)
        return ""
    return canvas_id


async def _drop_create_intent(channel: str) -> None:
    """Clear the stored create intent once no create of ours can have landed.

    Best-effort: a kept intent only lets a later "already exists" check the
    channel canvas, which is still adopted only when this bot made it.
    """
    _maybe_created.pop(channel, None)
    try:
        await surface_update(channel, canvas_create_pending_since=0.0)
    except Exception:
        logger.info("code channel %s: canvas create intent not cleared", channel, exc_info=True)


async def _publish_canvas_locked(call: Call, channel: str, markdown: str, title: str) -> str:
    record = await surface_get(channel)
    canvas_id = (record.plan_canvas_id if record is not None else "") or _unsaved_canvas.get(
        channel, ""
    )
    created = False
    if not canvas_id:
        # Read before this create: only an EARLIER create's lost answer counts,
        # and when it began bounds which canvas could be its.
        lost_began = _maybe_created.get(channel) or (
            record.canvas_create_pending_since if record is not None else 0.0
        )
        # The earliest create that may have landed: a second lost answer must not
        # push the bound past a canvas the first one made.
        began = min(lost_began, time.time()) if lost_began else time.time()
        # Stored BEFORE the create and kept until the canvas id is: whatever is
        # lost after this point (the answer, the id write, the process), the
        # next "already exists" can tell the canvas may be ours. A create whose
        # intent cannot be stored is not attempted (the write raises).
        _commit(channel)
        await surface_update(channel, canvas_create_pending_since=began)
        _commit(channel)  # a stored intent alone adopts nothing that isn't ours
        _maybe_created[channel] = began
        try:
            canvas_id = await _create_canvas(call, channel, markdown, title, began)
            created = True
        except CodeChannelApiError as exc:
            if _may_have_landed(exc):
                raise  # the stored intent above already covers it
            if not lost_began:
                # Slack refused this create outright and no earlier one may have
                # landed: no canvas of ours can exist, so the intent goes.
                await _drop_create_intent(channel)
            if exc.code != CANVAS_ALREADY_EXISTS:
                raise
            # Adopted only when a create of ours may have made it (its answer was
            # lost); otherwise never retried and never adopted: a canvas this
            # gateway did not create may be a shared document, and writing it
            # would edit it everywhere it appears.
            canvas_id = await _adopt_lost_create(call, channel, lost_began) if lost_began else ""
            if not canvas_id:
                raise ToolRefusal(
                    "Error: this channel already has a canvas that Kiro Crew did not create, "
                    "so it was left alone. Share the plan as an html view instead.",
                    CANVAS_ALREADY_EXISTS,
                ) from None
        if not canvas_id:
            raise CodeChannelApiError("conversations.canvases.create", "missing_canvas_id")
        _unsaved_canvas[channel] = canvas_id
    if _unsaved_canvas.get(channel) == canvas_id:
        # Held in memory until stored: a failed write raises, and the next call
        # stores it instead of creating a second canvas.
        await surface_update(channel, plan_canvas_id=canvas_id, canvas_create_pending_since=0.0)
        _unsaved_canvas.pop(channel, None)
        _maybe_created.pop(channel, None)
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
        if _redact_payload(key) != key:
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
            if _redact_payload(url) != url:
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
    # One read-update-publish-rollback at a time per channel: a slow call's
    # rollback can then never land on top of a newer call's items.
    async with _bar_locks.setdefault(channel, asyncio.Lock()):
        await _publish_bar_locked(call, channel, items, agent_bar, workspace)
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


#: Per-channel context-bar transaction locks.
_bar_locks: dict[str, asyncio.Lock] = {}


async def _publish_bar_locked(
    call: Call, channel: str, items: list[dict[str, str]], agent_bar: Any, workspace: Any
) -> None:
    previous = await surface_get(channel)
    before = previous.context_bar if previous else ()
    # Not stored once stopped: a background republish would publish stored items.
    _commit(channel)
    # The store write runs on in its thread whatever is cancelled: wait for it,
    # so a cancel here rolls back what was really stored, not nothing.
    writing, cancelled = await _settle_future(surface_update(channel, context_bar=items))
    if cancelled:
        if not writing.cancelled() and writing.exception() is None:
            await _settle(_rollback_bar(channel, writing.result().context_bar, before))
        raise asyncio.CancelledError
    stored = writing.result().context_bar  # raises the write's own error
    service = getattr(workspace, "_SERVICE", None)

    async def publish() -> None:
        result = await service.republish_bar(call.binding) if service is not None else None
        if result is None:
            # No workspace record to publish from: the bar is the agent's items alone.
            merged, _ = agent_bar.merge([], items)
            await call.client.set_context_bar(channel, merged)
        elif result:
            raise CodeChannelApiError("agents.conversations.setProperties", result)

    # Waited out through cancels: the stored items always end up matching what
    # the channel shows, and every step finishes inside the caller's lock.
    publishing, cancelled = await _settle_future(publish())
    if publishing.cancelled() or publishing.exception() is not None:
        # Roll back only what this call stored: never someone else's newer items.
        cancelled = await _settle(_rollback_bar(channel, stored, before)) or cancelled
        if not cancelled:
            publishing.result()  # re-raises the publish's own error
    if cancelled:
        raise asyncio.CancelledError


async def _settle_future(coro: Any) -> tuple[asyncio.Future[Any], bool]:
    """Run *coro* to its end through any cancels: ``(its future, was cancelled)``.

    The one shape every step of a context-bar call uses, so a cancel can never
    land between a step and the state it leaves (the class GPT keeps raising).
    """
    fut = asyncio.ensure_future(coro)
    cancelled = False
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            cancelled = True
    return fut, cancelled


async def _settle(coro: Any) -> bool:
    """:func:`_settle_future` for a cleanup step: logs its failure; True if cancelled."""
    fut, cancelled = await _settle_future(coro)
    if not fut.cancelled() and fut.exception() is not None:
        logger.warning("code channel: context-bar step failed", exc_info=fut.exception())
    return cancelled


async def _rollback_bar(channel: str, ours: tuple, before: tuple) -> None:
    current = await surface_get(channel)
    if current is not None and current.context_bar == ours:
        await surface_update(channel, context_bar=before)


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
    # A Stop during the send already cleared any hold: never put one back.
    _commit(call.binding.channel_id)
    channel_status.hold_after_turn(call.binding.channel_id, value if value == "suspended" else None)
    if value == "suspended":
        return "Status set to suspended: the channel shows you are waiting on its people, and stays so after this turn."
    return f"Status set to {value}."


async def _wrap_up(call: Call, args: dict[str, Any]) -> str:
    _only(args, {"summary"}, WRAP_UP)
    from kiro_crew.slack.code_channels.wrapup import MAX_SUMMARY_CHARS, post_wrap_up

    summary = _text(args, "summary", required=True, max_len=MAX_SUMMARY_CHARS)
    from kiro_crew.slack.code_channels.wrapup import carried_request

    # Settles the /wrap-up whose turn this is; in an ordinary turn, none.
    ts = await post_wrap_up(
        call.binding,
        summary,
        caller=call.caller,
        settles=carried_request(call.binding.channel_id),
    )
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
