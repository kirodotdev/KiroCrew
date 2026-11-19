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

from kiro_crew.validation_parts.field_engine import FieldSpec, ToolSchema

#: The agent's handle for an ask. A declaration with a pinned key replaces that ask.
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TITLE_MAX = 120
DETAIL_MAX = 1000
OPTIONS_MAX = 6
LABEL_MAX = 200
DESCRIPTION_MAX = 500
#: Live asks one session holds at once; a fifth new key is refused.
MAX_ASKS = 4

#: The server-minted id of one declaration: a change to an ask mints a new one.
ASK_ID_RE = re.compile(r"^wy-[0-9a-f]{16}$")
#: The client-chosen id of one answer click, reused when that click is retried.
ANSWER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
#: The longest typed answer an action card sends, the composer's own message bound.
ANSWER_TEXT_MAX = 32768
#: The text an action card's answer sends when nothing was typed.
EMPTY_ACTION_ANSWER = "Done"

#: ``state`` of an ask nothing is unsure about.
STATE_PINNED = "pinned"
#: ``state`` of an ask whose answer was being sent when the gateway stopped.
STATE_UNCERTAIN = "uncertain"

#: Refusal codes the owner routes answer with (HTTP 409 unless noted).
CODE_SUPERSEDED = "superseded"
CODE_ANSWERED = "answered"
CODE_CLEARED = "cleared"
CODE_UNCERTAIN = "uncertain"
#: HTTP 400: a label that is not one of the stored options, or a malformed body.
CODE_INVALID_ANSWER = "invalid_answer"
#: A fifth new key while four are pinned.
CODE_TOO_MANY_ASKS = "too_many_asks"
CODE_INVALID_ASK = "invalid_ask"

WAITING_ON_YOU_OPTION_SCHEMA = ToolSchema(
    tool_name="waiting_on_you.option",
    fields=[
        FieldSpec("label", str, required=True, max_len=LABEL_MAX),
        FieldSpec("description", str, max_len=DESCRIPTION_MAX),
    ],
)

WAITING_ON_YOU_SCHEMA = ToolSchema(
    tool_name="waiting_on_you",
    fields=[
        FieldSpec("key", str, required=True, max_len=40),
        FieldSpec("title", str, required=True, max_len=TITLE_MAX),
        FieldSpec("detail", str, max_len=DETAIL_MAX),
        FieldSpec(
            "options",
            list,
            item_type=dict,
            max_items=OPTIONS_MAX,
            item_schema=WAITING_ON_YOU_OPTION_SCHEMA,
        ),
    ],
)

WAITING_ON_YOU_CLEAR_SCHEMA = ToolSchema(
    tool_name="waiting_on_you_clear",
    fields=[
        FieldSpec("key", str, max_len=40),
        FieldSpec("all", bool),
    ],
)


class AskError(Exception):
    """A declaration, answer or dismissal the gateway refuses, with its HTTP status."""

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
        description = _text(
            raw.get("description"), "option description", DESCRIPTION_MAX, required=False
        )
        options.append({"label": label, "description": description})
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise AskError(400, CODE_INVALID_ASK, "option labels must differ")
    return {"key": key, "title": title, "detail": detail, "options": options}
