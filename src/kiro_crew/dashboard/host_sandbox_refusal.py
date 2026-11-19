"""A chat the Crew's host could not start because it cannot build an OS sandbox.

``sandbox.wrap_argv`` refuses the agent spawn with ``SandboxUnavailableError`` when
the host has no sandbox backend and unsandboxed execution is not allowed, and the
ACP transport raises ``AcpSandboxInitFailed`` when a backend exists but the
sandbox launcher fails as the agent starts (its stderr tail and the remedy
switch are in the message). Their
message is the host operator's runbook: the probe's errno, the kernel sysctl, the
host's OS family, ``kirocrew service install`` and the owner's config key. The
machine owner keeps that text. Anyone else reading the chat (a Shared Crew
teammate, an app token) cannot act on it and should not learn the host's setup,
so their copy of the row says only that the host cannot start chats.

The row is typed by :data:`HOST_SANDBOX_UNAVAILABLE`, in ``meta.code`` and as the
content's ``<code>: `` prefix (the error-row convention of ``memory_unavailable``):
the chat SSE stream carries no row meta, so the prefix is what types it there. The
dashboard hides the prefix (``chatErrorRecovery.chatErrorDisplayText``).

:func:`for_non_owner` is applied on the edges ``redaction_audience`` already owns
for a reader who is not the machine owner: the websocket send, the chat SSE writer,
the status stream and every HTTP response body (:func:`body_for_non_owner`).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from kiro_crew.dashboard.gateway_notice import GATEWAY_NOTICE_ARGS_KEY, GATEWAY_NOTICE_META_KEY

#: ``meta.code`` and the content prefix of the row.
HOST_SANDBOX_UNAVAILABLE = "host_sandbox_unavailable"

_PREFIX = f"{HOST_SANDBOX_UNAVAILABLE}:"
_CODE_BYTES = HOST_SANDBOX_UNAVAILABLE.encode()

#: The wording of a refusal row a build without the typed prefix stored. Those rows
#: stay in transcripts, so they are read by their wording too. Each text is fixed by
#: its raiser: every ``SandboxUnavailableError`` message opens with the first (the
#: probe's errno and the owner's config key follow), and ``AcpSandboxInitFailed``'s
#: carries the second (the host's path and switch follow). A stored row holds it
#: behind a lead (the card's glyph, ``Error:``) or as the last error of the
#: background-start stop notice (``eager_spawn_stopped``), so it is looked for
#: anywhere in the row's content.
_UNTYPED_MARKS = (
    "Sandbox backend unavailable",
    "sandbox failed to initialize, so the agent process could not start",
)
_UNTYPED_BYTES = tuple(mark.encode() for mark in _UNTYPED_MARKS)

#: What a reader who is not the machine owner receives in place of the diagnostic.
#: Fixed text with no host detail; the dashboard shows its own translated line.
NON_OWNER_TEXT = (
    f"{_PREFIX} This Crew's host can't start chats right now. Ask the Crew's owner to check it."
)

#: Row meta keys that may carry the diagnostic: a gateway notice's token and its
#: interpolation values (the background-start stop row names the last error there).
_DETAIL_META_KEYS = (GATEWAY_NOTICE_META_KEY, GATEWAY_NOTICE_ARGS_KEY)

#: How deep the cause chain is walked for a wrapped refusal.
_MAX_CHAIN = 8


def host_sandbox_refusal(exc: BaseException | None) -> Any:
    """The ``SandboxUnavailableError`` or ``AcpSandboxInitFailed`` in *exc*'s
    cause chain, or None."""
    from kiro_crew.agent_sdk.drivers.acp_vocab import is_sandbox_init_failure
    from kiro_crew.sandbox import SandboxUnavailableError

    seen: set[int] = set()
    for _ in range(_MAX_CHAIN):
        if exc is None or id(exc) in seen:
            return None
        if isinstance(exc, SandboxUnavailableError) or is_sandbox_init_failure(exc):
            return exc
        seen.add(id(exc))
        # An explicit cause, else the implicit context unless `raise ... from None` hid it.
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return None


def row_meta(refusal: Any) -> dict[str, object]:
    """The typed meta for the error row of *refusal*: the code and when it happened.

    ``retryable`` marks a momentary refusal (``kind == "transient"``), which a
    retry can clear, so the card keeps Resume for it.
    """
    meta: dict[str, object] = {
        "code": HOST_SANDBOX_UNAVAILABLE,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if getattr(refusal, "kind", "") == "transient":
        meta["retryable"] = True
    return meta


def row_text(text: str) -> str:
    """*text*, the owner's diagnostic, with the row's typed prefix."""
    return f"{_PREFIX} {text}"


def is_refusal_row(value: dict[str, Any]) -> bool:
    """Whether *value*, one transcript row, is a host-sandbox refusal (typed or untyped)."""
    if value.get("role") != "error" and value.get("type") != "error":
        return False
    meta = value.get("meta")
    if isinstance(meta, dict) and meta.get("code") == HOST_SANDBOX_UNAVAILABLE:
        return True
    content = value.get("content")
    return isinstance(content, str) and (
        content.startswith(_PREFIX) or _names_untyped_refusal(content)
    )


def _names_untyped_refusal(text: str) -> bool:
    return any(mark in text for mark in _UNTYPED_MARKS)


def _may_hold_row(data: bytes) -> bool:
    return _CODE_BYTES in data or any(mark in data for mark in _UNTYPED_BYTES)


def for_non_owner(value: Any) -> Any:
    """*value*, a decoded JSON document, with every such row as a non-owner reads it.

    The content becomes :data:`NON_OWNER_TEXT` and the meta loses the keys that
    can carry the diagnostic and carries the row's code. *value* itself is returned when nothing matched.
    """
    if isinstance(value, list):
        items = [for_non_owner(item) for item in value]
        return value if all(a is b for a, b in zip(items, value)) else items
    if not isinstance(value, dict):
        return value
    if is_refusal_row(value):
        shown = dict(value)
        shown["content"] = NON_OWNER_TEXT
        meta = value.get("meta")
        kept = (
            {k: v for k, v in meta.items() if k not in _DETAIL_META_KEYS}
            if isinstance(meta, dict)
            else {}
        )
        # Typed, so the dashboard draws the host card (one line, Copy details)
        # even for a row an older build stored without the code.
        shown["meta"] = {**kept, "code": HOST_SANDBOX_UNAVAILABLE}
        return shown
    changed = {key: for_non_owner(item) for key, item in value.items()}
    return value if all(changed[k] is value[k] for k in value) else changed


def body_for_non_owner(body: bytes) -> bytes | None:
    """A response *body* for a non-owner reader, or None when it holds no such row.

    Read as one JSON document, else as JSON lines (an archived transcript), so the
    rule follows the code's bytes, not the body's content type. The code's bytes
    are looked for first, so a body without them is never decoded. A line that is
    not JSON is no row and is left as it is (a source file that names the code).
    """
    if not _may_hold_row(body):
        return None
    try:
        decoded = json.loads(body)
    except (ValueError, RecursionError):
        return _lines_for_non_owner(body)
    shown = for_non_owner(decoded)
    if shown is decoded:
        return None
    return json.dumps(shown).encode()


def _lines_for_non_owner(body: bytes) -> bytes | None:
    lines = body.split(b"\n")
    changed = False
    for i, line in enumerate(lines):
        if not _may_hold_row(line):
            continue
        try:
            decoded = json.loads(line)
        except (ValueError, RecursionError):
            continue
        shown = for_non_owner(decoded)
        if shown is not decoded:
            lines[i] = json.dumps(shown).encode()
            changed = True
    return b"\n".join(lines) if changed else None


def text_for_non_owner(text: str) -> str:
    """A JSON frame or event *text* for a non-owner reader; *text* itself when it
    holds no such row (or is not JSON)."""
    if HOST_SANDBOX_UNAVAILABLE not in text and not _names_untyped_refusal(text):
        return text
    shown = body_for_non_owner(text.encode())
    return text if shown is None else shown.decode()
