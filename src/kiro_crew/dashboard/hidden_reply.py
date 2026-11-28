"""Which assistant rows the chat page draws as nothing.

The backend twin of ``isHiddenReplyAt`` and ``turnHadPolicyBlock`` in
``website/src/app-sdk/turnPolicyBlock.ts`` (with ``isHiddenInvisibleAssistantRow``
in ``website/src/utils/invisibleText.ts``). Regenerate's target scan
(``chat_regenerate``) asks it so the server regenerates the reply the dashboard
put the Regenerate button on, not a hidden row after it.

The two sides judge the same rows: the dashboard judges the rows
``_prepare_messages`` sends, and the handler hands this module the same
projection (``wire_rows.WireRows``). Every text rule here copies the
dashboard's, not the backend's own: JavaScript's whitespace set, the dashboard's
``[STEERING`` grammar (``app-sdk/protocol/steering.ts``) and JavaScript
truthiness. ``test/fixtures/hidden_reply_rows.json`` holds both sides to one
verdict per row; a change to either side's rule needs a row there.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence

from kiro_crew.preview_text import drop_format_chars

#: ECMAScript's WhiteSpace and LineTerminator code points: the set ``\\s`` and
#: ``String.prototype.trim`` use. Python's ``str.strip`` and ``\\s`` differ (they add
#: U+001C-U+001F and U+0085 and leave out U+FEFF).
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_TRIM_RE = re.compile(rf"^[{_JS_WS}]+|[{_JS_WS}]+$")

#: ``STEER_ACK_HEAD_RE`` in ``website/src/app-sdk/protocol/steering.ts``: case-sensitive
#: and a ``:`` required. Deliberately not ``messaging.driver.strip_steering_markers``,
#: whose grammar differs (case-insensitive, hex id, optional ``:``).
_DASHBOARD_STEER_HEAD_RE = re.compile(rf"\[STEERING[{_JS_WS}]+steer-[^\]:]+:")


def _strip_dashboard_notes(content: str) -> str:
    """``extractSteeringAcks(content).cleaned``, up to the blank-line collapse and
    ``trimEnd`` that cannot change whether the text is empty.

    A note ends at the ``]`` that balances its own ``[`` (a summary may hold a
    markdown link), else at the first ``]`` after its head. A head past the last
    ``]`` or inside a note already removed is left alone, as there.
    """
    last_close = content.rfind("]")
    closers: dict[int, int] = {}
    opened: list[int] = []
    for i, char in enumerate(content):
        if char == "[":
            opened.append(i)
        elif char == "]" and opened:
            closers[opened.pop()] = i
    out: list[str] = []
    pos = 0
    for head in _DASHBOARD_STEER_HEAD_RE.finditer(content):
        start = head.start()
        if start > last_close:
            break
        if start < pos:
            continue
        close = closers.get(start)
        if close is None:
            close = content.find("]", head.end())
        out.append(content[pos:start])
        pos = close + 1
    out.append(content[pos:])
    return "".join(out)


def _js_trim(text: str) -> str:
    return _JS_TRIM_RE.sub("", text)


def _js_truthy(value: object) -> bool:
    """JavaScript's truthiness: only ``undefined``/``null``, ``false``, ``0``,
    ``NaN`` and ``""`` are false; an empty array or object is true."""
    if value is None or value is False or value == "":
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    return True


def _invisible(text: str, *, strip_notes: bool) -> bool:
    """``isInvisibleOnly``, after ``extractSteeringAcks`` when *strip_notes*."""
    if strip_notes:
        text = _strip_dashboard_notes(text)
    return _js_trim(drop_format_chars(text)) == ""


def _hidden_row(row: Mapping[str, object], *, strip_notes: bool) -> bool:
    """``isHiddenInvisibleAssistantRow`` on a surface that draws file chips."""
    if row.get("role") != "assistant":
        return False
    meta = row.get("meta")
    if isinstance(meta, Mapping):
        changes = meta.get("file_changes")
        if isinstance(changes, list) and changes:
            return False
    variants = row.get("variants")
    if isinstance(variants, list):
        for variant in variants:
            if not isinstance(variant, Mapping):
                continue
            text = variant.get("content")
            if isinstance(text, str) and not _invisible(text, strip_notes=strip_notes):
                return False
    content = row.get("content")
    if not isinstance(content, str):
        # A row with no ``content`` key leaves ``_prepare_messages`` without one,
        # and the dashboard cannot judge it. It stays a Regenerate candidate.
        return False
    return _invisible(content, strip_notes=strip_notes)


def turn_had_policy_block(messages: Sequence[Mapping[str, object]], index: int) -> bool:
    """``turnHadPolicyBlock``: a tool-blocked inject, or a row carrying a host
    approval decline's cause, sits between row *index* (itself included) and the
    nearest ``user`` row, and that row is not a steer.

    The walk ends only at a ``user`` row, so it passes turn-opening inject rows,
    and a taken-back steer still counts as a steer (it keeps ``meta.steer``).
    Both quirks are the twin's, kept on purpose.
    """
    from kiro_crew.dashboard.chat_utils import HOST_DECLINE_CAUSE_META_KEY
    from kiro_crew.dashboard.state_parts.injected_turns import REFUSAL_INBAND_RECOVERY_PREFIX

    blocked = False
    for i in range(min(index, len(messages) - 1), -1, -1):
        row = messages[i]
        if not isinstance(row, Mapping):
            continue
        role = row.get("role")
        if role == "user":
            meta = row.get("meta")
            if isinstance(meta, Mapping) and _js_truthy(meta.get("steer")):
                return False
            break
        if role == "inject":
            content = row.get("content")
            if isinstance(content, str) and content.startswith(REFUSAL_INBAND_RECOVERY_PREFIX):
                blocked = True
        meta = row.get("meta")
        if isinstance(meta, Mapping) and _js_truthy(meta.get(HOST_DECLINE_CAUSE_META_KEY)):
            blocked = True
    return blocked


def is_hidden_reply_at(messages: Sequence[Mapping[str, object]], index: int) -> bool:
    """``isHiddenReplyAt``: assistant row *index* draws nothing on the chat page.

    Either its text is invisible-only, or it is only a ``[STEERING …]`` note in a
    turn whose Steered chip ``turn_had_policy_block`` drops.
    """
    row = messages[index]
    if not _hidden_row(row, strip_notes=True):
        return False
    return _hidden_row(row, strip_notes=False) or turn_had_policy_block(messages, index)
