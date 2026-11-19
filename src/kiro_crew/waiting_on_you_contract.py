"""The waiting-on-you ask: its limits, its identifiers and the check a declaration passes.

An agent blocked on the person declares an ask with the ``waiting_on_you`` tool, and
the dashboard pins it above the composer until it is answered, cleared or expires
(``src/kiro_crew/docs/waiting-on-you.md``). Both halves read the
bounds from here: the MCP tool for its schema, the gateway for the authoritative
check before anything is stored. Over-length text is refused rather than cut, so the
agent knows exactly what the person sees.

A leaf module: the MCP server imports it without the dashboard.
"""

from __future__ import annotations

import re
from typing import Any

from kiro_crew.validation_parts import core_turn_schemas as _schemas

# The text bounds and the two tool schemas are defined beside the other core tool
# schemas, so the schema registry a tool listing loads does not load this module.
TITLE_MAX = _schemas.WAITING_ON_YOU_TITLE_MAX
DETAIL_MAX = _schemas.WAITING_ON_YOU_DETAIL_MAX
OPTIONS_MAX = _schemas.WAITING_ON_YOU_OPTIONS_MAX
LABEL_MAX = _schemas.WAITING_ON_YOU_LABEL_MAX
DESCRIPTION_MAX = _schemas.WAITING_ON_YOU_DESCRIPTION_MAX
WAITING_ON_YOU_SCHEMA = _schemas.WAITING_ON_YOU_SCHEMA
WAITING_ON_YOU_CLEAR_SCHEMA = _schemas.WAITING_ON_YOU_CLEAR_SCHEMA

#: The agent's handle for an ask. A declaration with a pinned key replaces that ask.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
#: Live asks one session holds at once; a fifth new key is refused.
MAX_ASKS = 4

#: The server-minted id of one declaration: a change to an ask mints a new one.
ASK_ID_RE = re.compile(r"^wy-[0-9a-f]{16}$")
#: Refusal codes the owner's dismiss route answers with (HTTP 409 unless noted).
CODE_SUPERSEDED = "superseded"
CODE_CLEARED = "cleared"
#: HTTP 400: a malformed dismiss body.
CODE_INVALID_ANSWER = "invalid_answer"
#: A fifth new key while four are pinned.
CODE_TOO_MANY_ASKS = "too_many_asks"
CODE_INVALID_ASK = "invalid_ask"

#: Characters the send path trims before reading a label's first character, as the
#: inclusive code-point ranges of ``DISPATCH_TRIMMED_RANGES`` in ``dispatchShape.ts``.
#: ``str.strip`` keeps some of them (U+200B-U+200D, U+2060, U+FEFF), so a sigil behind
#: one would otherwise pass. ``test_waiting_on_you_agent`` holds the two tables equal.
DISPATCH_TRIMMED = "".join(
    chr(code_point)
    for first, last in (
        (0x0009, 0x000D),
        (0x001C, 0x0020),
        (0x0085, 0x0085),
        (0x00A0, 0x00A0),
        (0x1680, 0x1680),
        (0x2000, 0x200D),
        (0x2028, 0x2029),
        (0x202F, 0x202F),
        (0x205F, 0x2060),
        (0x3000, 0x3000),
        (0xFEFF, 0xFEFF),
    )
    for code_point in range(first, last + 1)
)
#: Leading characters that make a sent label a harness command or a prompt mention.
DISPATCH_SIGILS = ("/", "@")
#: The fixed opening of a paste token, which the composer's send expands into the block
#: it names, so the label the person read would not be the text that goes out.
DISPATCH_PASTE_TOKEN = "[ Paste #"


class AskError(Exception):
    """A declaration or dismissal the gateway refuses, with its HTTP status."""

    def __init__(self, status: int, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "error": self.message, **self.extra}


def _text(value: Any, field: str, limit: int, *, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise AskError(400, CODE_INVALID_ASK, f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise AskError(400, CODE_INVALID_ASK, f"{field} is required")
    if len(text) > limit:
        raise AskError(
            400,
            CODE_INVALID_ASK,
            f"{field} is {len(text)} characters; at most {limit} are shown, so shorten it",
        )
    return text


def valid_key(value: Any) -> bool:
    return isinstance(value, str) and bool(KEY_RE.fullmatch(value))


def _dispatches_as_more_than_prose(label: str) -> bool:
    """Whether *label*, sent as the person's turn, would run a command or load a skill.

    Mirrors ``dispatchIsCommandShaped`` in ``dispatchShape.ts``: a sigil counts only at
    the start, so "Run the a/b test" stays prose, while a ``$skill`` token expands at any
    offset, and so does a paste token.
    """
    # Imported here: ``kiro_crew.skills`` imports this module, so a top-level import is a
    # cycle, and the schema registry that loads this leaf would pay for the whole loader.
    from kiro_crew.skills import _DOLLAR_SKILL_PATTERN

    dispatched = label.lstrip(DISPATCH_TRIMMED)
    return (
        dispatched.startswith(DISPATCH_SIGILS)
        or bool(_DOLLAR_SKILL_PATTERN.search(dispatched))
        or DISPATCH_PASTE_TOKEN in dispatched
    )


def validate_declaration(args: Any) -> dict[str, Any]:
    """The ask *args* declares, checked and stripped, or :class:`AskError` (400).

    The returned dict holds ``key``, ``title``, ``detail`` and ``options`` (a list of
    ``{"label", "description"}``). Nothing is truncated.
    """
    if not isinstance(args, dict):
        raise AskError(400, CODE_INVALID_ASK, "the ask must be a JSON object")
    key = args.get("key")
    if not valid_key(key):
        raise AskError(
            400,
            CODE_INVALID_ASK,
            "key must be 1-40 characters of a-z, 0-9 and '-', starting with a letter or digit",
        )
    title = _text(args.get("title"), "title", TITLE_MAX, required=True)
    if "\n" in title or "\r" in title:
        raise AskError(400, CODE_INVALID_ASK, "title must be one line")
    detail = _text(args.get("detail"), "detail", DETAIL_MAX, required=False)
    raw_options = args.get("options")
    if raw_options is None:
        raw_options = []
    if not isinstance(raw_options, list):
        raise AskError(400, CODE_INVALID_ASK, "options must be a list")
    if len(raw_options) > OPTIONS_MAX:
        raise AskError(400, CODE_INVALID_ASK, f"at most {OPTIONS_MAX} options")
    options: list[dict[str, str]] = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            raise AskError(400, CODE_INVALID_ASK, "each option must be an object with a label")
        unknown = set(raw) - {"label", "description"}
        if unknown:
            raise AskError(400, CODE_INVALID_ASK, f"unknown option field {sorted(unknown)[0]!r}")
        label = _text(raw.get("label"), "option label", LABEL_MAX, required=True)
        if _dispatches_as_more_than_prose(label):
            raise AskError(
                400,
                CODE_INVALID_ASK,
                f"option label {label!r} would run as a command or load a skill when "
                "chosen; reword it as plain text, with no leading '/' or '@' and no "
                "$name or '[ Paste #' token",
            )
        description = _text(
            raw.get("description"), "option description", DESCRIPTION_MAX, required=False
        )
        options.append({"label": label, "description": description})
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise AskError(400, CODE_INVALID_ASK, "option labels must differ")
    return {"key": key, "title": title, "detail": detail, "options": options}
