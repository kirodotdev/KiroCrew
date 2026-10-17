"""What a dashboard turn's tool rows persist and broadcast: row meta, the WS payload, the
refinement rewrite, the tool-call identity key and the rows-only save."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _MAX_TOOL_PURPOSE,
        LLMEvent,
        UnknownMemoryStore,
        _redact_tool_field,
        _tool_identity_fields,
        logger,
        redact_credentials,
        redact_exfiltration_urls,
        restore_replacement_if_handover_did_not_land,
        save_slot_off_loop,
        slot_history_key,
        tighten_replacement_to_restricted_original,
    )


async def _persist_tool_result_rows(state: Any, slot: Any) -> Any:
    """Persist tool rows without leaving a same-file replacement looser."""
    history_key = slot_history_key(slot)
    try:
        tightening = tighten_replacement_to_restricted_original(state, slot.key, slot)
    except UnknownMemoryStore:
        tightening = None
        logger.warning(
            "Slot %s: replacement rebound twice during tightening; writing the tail "
            "under the ratcheted line without tightening the live replacement",
            slot.key,
        )
    try:
        committed = await save_slot_off_loop(state, slot, rows_only=True)
    except Exception:
        await restore_replacement_if_handover_did_not_land(state, slot.key, tightening, history_key)
        raise
    if not committed:
        await restore_replacement_if_handover_did_not_land(state, slot.key, tightening, history_key)
    return committed


def _refined_tool_row_content(existing: str, new_title: str) -> str | None:
    """The rewritten content for a tool row a ``tool_call_update`` refines, or None.

    kiro-cli sends the resolved title after a permission is answered, and the row
    is re-rendered as ``"<icon> <title>"``, preserving whichever leading icon the
    row already carries (🔧 running / ✅ done / 🚫 refused).

    Returns None for a REFUSAL row, which must be left alone: it is terminal (the
    tool will not run, so a better title adds nothing) and its content is
    ``"🚫 <title> — <reason>"``, so re-rendering from the title alone deletes the
    reason. That tail is the user's only visible explanation, and the model has
    already been told the reason in-band — dropping it leaves the human looking at
    a blocked row with no cause while the agent acts on one they cannot see.
    """
    prefix = existing[:1] if existing[:1] in ("🔧", "✅", "🚫") else "🔧"
    if prefix == "🚫":
        return None
    return f"{prefix} {new_title}"


# Distinct redacted tool_call_ids one turn tracks a source digest for. The ids
# come from the LLM, so their number is not the runner's to trust; past this a
# further id is treated as collapsed, which withholds its app notice rather than
# letting the turn's state grow with the stream. Far above any real turn's tool
# count, so a genuine conversation never reaches it.
_MAX_TCID_SOURCES = 512


# Bytes of a redacted tool_call_id this turn will track an identity for. The ids
# come from the LLM, so neither their number nor their LENGTH is the runner's to
# trust, and a bound on the count alone still retains 512 unbounded strings. Well
# under `_MAX_TOOL_FIELD`, so a tracked id is never one `_redact_tool_field`
# truncates -- two different over-long ids share a truncated prefix, so a
# truncated id cannot be told from another and must not be tracked at all.
_MAX_TCID_LEN = 1024


def _tcid_identity_key(tcid: str | None) -> str:
    """Bounded key for a redacted ``tool_call_id``, or ``""`` when it names nothing.

    The key is a digest, so what the turn retains is 16 characters however long the
    id was. It is taken over ``_redact_tool_field``'s output -- the same call the
    reader makes -- because that, not the raw field, is the value rows are stored
    and compared under, and deriving the key with the reader's own function is what
    makes the two ends agree by construction rather than by assumption.

    An absent or over-long id returns ``""``, which no caller treats as identifying,
    so both fail towards withholding the notice.
    """
    if not tcid:
        return ""
    if len(tcid.encode("utf-8", "replace")) > _MAX_TCID_LEN:
        return ""
    canonical = _redact_tool_field(tcid)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def _tool_meta(event: "LLMEvent") -> dict[str, str] | None:
    """Build the meta dict persisted on a tool message — `tool_call_id`,
    `purpose`, and the full redacted `input`. Output is appended later by the
    EVENT_TOOL_RESULT handler. The inline detail panel is the only source of
    truth for what an agent ran, so the meta carries the full content (capped
    at 1 MB and 8 KB respectively as defensive safety nets).

    `tool_call_id` is redacted to match `_broadcast_auto_tool` in
    `chat_utils.py`, which has always redacted before the WS broadcast.
    Keeping it consistent across persisted meta and live broadcast means the
    frontend join (`toolLog[i].tool_call_id` ↔ `message.meta.tool_call_id`)
    works whether the entry came from the live tool-call event or from a
    historical replay. Comparison sites (e.g. EVENT_TOOL_RESULT) must
    redact `event.tool_call_id` before matching against the stored value."""
    if not event.tool_call_id:
        return None
    return {
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input": _redact_tool_field(event.tool_input),
        # ACP tool kind (read/edit/execute/…). The dashboard gates the inline
        # diff-card promotion on kind == "edit" so a shell command whose input
        # happens to look like a diff is never promoted; persisting it keeps
        # historical rows gate-able identically to live ones.
        "kind": _redact_tool_field(event.tool_kind, limit=64),
        # Trusted tool identity from ``_meta.kiro``, present only when the backend
        # sent one (``_tool_identity_fields`` omits rather than writes empty). The
        # dashboard's title derivation reads these for MCP calls so a replayed row
        # does not have to parse ``@server/tool`` out of the title; both are
        # optional there, so a row without them still renders.
        **_tool_identity_fields(event),
    }


def _tool_call_ws_payload(event: "LLMEvent") -> dict[str, str | bool]:
    """Build the live dashboard payload for a tool invocation.

    ``is_shell`` is intentionally an explicit capability signal rather than a
    frontend guess based on the tool title. Shell commands usually have no
    trustworthy total, so the dashboard can render an indeterminate status
    today while future tools can add a real progress mode without changing the
    tool-card data flow.
    """
    title, _ = redact_exfiltration_urls(event.title)
    title, _ = redact_credentials(title)
    kind, _ = redact_exfiltration_urls(event.tool_kind)
    kind, _ = redact_credentials(kind)
    return {
        "slot": "",  # Filled by the caller because it belongs to the session.
        "tool": title,
        "kind": kind,
        "is_shell": event.is_shell,
        "tool_call_id": _redact_tool_field(event.tool_call_id),
        "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
        "input_preview": _redact_tool_field(event.tool_input),
        **_tool_identity_fields(event),
    }
