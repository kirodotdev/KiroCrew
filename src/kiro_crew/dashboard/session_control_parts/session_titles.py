"""Session titles and colors: the hidden-character refusal, title cleaning for a new
and a renamed session, ``session_rename``, ``session_generate_title`` and
``session_set_color``.
"""

from __future__ import annotations

import asyncio
import logging
import unicodedata
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_title import (
    apply_manual_title,
    apply_regenerated_title,
    generate_manual_title,
)
from kiro_crew.dashboard.create_rate_limit import GENERATE_TITLE, WINDOW_SECS, allow_create
from kiro_crew.dashboard.handlers._shared import _scrub_text
from kiro_crew.dashboard.session_control_parts.caller_access import (
    SessionControlError,
    _audit,
    _audit_denials,
    _created_by_other,
    caller_slot_key,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _deny_factory,
    authorize_target,
)
from kiro_crew.dashboard.slot_color import write_slot_color
from kiro_crew.sel import sel
from kiro_crew.terminal_safe import _is_invisible  # one shared list of invisible code points
from kiro_crew.validation import SESSION_COLOR_CHOICES, SESSION_PALETTE_SIZE

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


#: Stored cap on a renamed title, the same ``[:200]`` the sidebar rename route and
#: :func:`create_session` keep. The MCP schema's own bound on the argument is the
#: generic ``MAX_SHORT_STRING`` (500), so :func:`_clean_rename_title` is what
#: refuses a title between 201 and 500 characters, for MCP and direct callers alike.
_MAX_RENAME_TITLE_CHARS = 200


#: Every Unicode text-direction control, refused in an agent-supplied title: the
#: embeddings, overrides and isolates (U+202A..U+202E, U+2066..U+2069) and the
#: directional marks (U+200E, U+200F, U+061C).
_BIDI_CONTROLS = frozenset(
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
)


#: Characters that render as a blank glyph yet are neither whitespace nor
#: invisible to :func:`kiro_crew.terminal_safe._is_invisible`, which already
#: covers format characters, variation selectors, the Hangul and Khmer fillers
#: and the tag block. The blank Braille pattern is the one such character: a
#: printable symbol that draws nothing.
_BLANK_GLYPHS = frozenset("\u2800")


#: Format characters (category ``Cf``) an agent-supplied title may contain: the zero-width
#: non-joiner and joiner, which emoji sequences and joined scripts such as Persian
#: and Devanagari need. The tag characters U+E0020..U+E007F are allowed
#: separately, by range; the tag block's other format character, U+E0001
#: LANGUAGE TAG, is refused. Its unassigned code points are not ``Cf`` and are
#: not refused by this rule.
_ALLOWED_FORMAT_CHARS = frozenset("\u200c\u200d")


def _is_allowed_format_char(ch: str) -> bool:
    """True for a format character an agent-supplied title keeps: a joiner, or a tag
    character (U+E0020..U+E007F), which spells the subdivision of a flag emoji
    such as England's. Every other ``Cf`` character is refused."""
    return ch in _ALLOWED_FORMAT_CHARS or "\U000e0020" <= ch <= "\U000e007f"


def _shows_nothing(text: str) -> bool:
    """True when every character of *text* is whitespace, invisible or a blank glyph."""
    return all(ch.isspace() or _is_invisible(ch) or ch in _BLANK_GLYPHS for ch in text)


def _refuse_hidden_title_characters(stripped: str, *, not_done: str) -> None:
    """Refuse a title whose characters hide, reorder or split what it shows.

    The character rules every agent-supplied sidebar title meets, in
    ``session_create``, ``session_fork`` and ``session_rename``: a model can send
    any code point, and the title is persisted and broadcast to every dashboard.
    *stripped* has had its surrounding whitespace removed and is not empty.
    *not_done* ends each refusal message ("session not renamed").
    """
    if any(unicodedata.category(ch) == "Cc" or ch in "\u2028\u2029" for ch in stripped):
        raise SessionControlError(
            f"title contains a control character; {not_done}",
            code="invalid_title",
            status=400,
        )
    # A direction control makes the sidebar render the label in an order other
    # than the one it was written in.
    if any(ch in _BIDI_CONTROLS for ch in stripped):
        raise SessionControlError(
            f"title contains a text-direction control; {not_done}",
            code="invalid_title",
            status=400,
        )
    # Any other format character (a zero-width space, a word joiner, a byte-order
    # mark, a soft hyphen) is invisible inside a visible title, so it can make a
    # near-duplicate of another session's label. Only the format characters that
    # emoji and joined scripts need are kept: see :func:`_is_allowed_format_char`.
    if any(unicodedata.category(ch) == "Cf" and not _is_allowed_format_char(ch) for ch in stripped):
        raise SessionControlError(
            f"title contains an invisible format character; {not_done}",
            code="invalid_title",
            status=400,
        )
    # A title needs at least one character that shows something. Invisible
    # characters (a zero-width space or joiner, a variation selector, a Hangul
    # filler) survive `str.strip()`, so a title made only of them, or of blank
    # glyphs, would store an invisible, final name the automatic titler can
    # never replace. Such characters are kept inside a visible title: a joiner
    # is how an emoji sequence is written.
    if _shows_nothing(stripped):
        raise SessionControlError(
            f"title has no visible character; {not_done}",
            code="invalid_title",
            status=400,
        )
    # A blank glyph is refused anywhere in the title, not only in an all-blank
    # one. `terminal_safe.normalize_for_scanning` does not remove it (it is a
    # printable symbol, not an invisible character), so one placed inside a
    # secret ("AKIA\u2800...") would split it past the redactors that
    # `_scrub_text` runs afterwards.
    if any(ch in _BLANK_GLYPHS for ch in stripped):
        raise SessionControlError(
            f"title contains a blank glyph (U+2800); {not_done}",
            code="invalid_title",
            status=400,
        )


def _clean_new_session_title(title: str, *, cap: int, not_done: str) -> str:
    """Validate the optional *title* of a session an agent opens; return the stored form.

    Used by :func:`create_session` and :func:`fork_session`. An empty or
    whitespace-only title is no title and returns ``""``, so the session keeps
    its automatic name. Any other title meets
    :func:`_refuse_hidden_title_characters`, then is scrubbed by
    ``handlers._shared._scrub_text``, which also redacts a secret an invisible
    character splits (``"AKIA\\u200d..."``), and is cut to *cap* characters.
    The cut comes after the scrub: cutting first could keep part of a secret
    that no redactor pattern matches any longer. A cut result that shows
    nothing (a long run of joiners ahead of the visible text) is refused.
    """
    stripped = title.strip()
    if not stripped:
        return ""
    _refuse_hidden_title_characters(stripped, not_done=not_done)
    cut = _scrub_text(stripped)[:cap]
    if _shows_nothing(cut):
        raise SessionControlError(
            f"title has no visible character; {not_done}",
            code="invalid_title",
            status=400,
        )
    return cut


def _clean_rename_title(title: str) -> str:
    """Validate *title* for :func:`rename_target` and return the stored form.

    The rules are the sidebar's: surrounding whitespace is stripped, an empty
    result is refused, and nothing over 200 characters is stored. Two refusals
    go further than the sidebar, because the sidebar's text box can produce
    neither case and a model can: a title over the cap is refused instead of cut
    down, and a control character (a newline, a tab, an escape) is refused
    because a sidebar title is one line.

    The stored form is the scrubbed title, which can differ from what the caller
    sent: ``handlers._shared._scrub_text`` replaces credential-shaped text with a
    longer marker, also catching a secret split by an invisible character. A
    title that grows past the cap that way is refused, never cut, so no stored
    title holds a torn marker. The verb's reply carries this stored form, so the
    caller reads back what the sidebar shows.
    """
    stripped = title.strip()
    if not stripped:
        raise SessionControlError("title is required", code="invalid_title", status=400)
    if len(stripped) > _MAX_RENAME_TITLE_CHARS:
        raise SessionControlError(
            f"title is longer than {_MAX_RENAME_TITLE_CHARS} characters; session not renamed",
            code="invalid_title",
            status=400,
        )
    _refuse_hidden_title_characters(stripped, not_done="session not renamed")
    # Sanitized with the stored-field scrub the dashboard handlers use: the title
    # is broadcast to every dashboard and persisted with the transcript. The
    # redactors match literal patterns, so an invisible character placed inside
    # a secret ("AKIA\u200d...", "AKIA\ufe0f...") would split it past the match;
    # `_scrub_text` also scans a normalized copy and keeps it when that copy
    # reveals a secret, and otherwise leaves invisible characters in place, so
    # an emoji sequence is unchanged. Its control-character step is inert here,
    # because a control character was refused above.
    scrubbed = _scrub_text(stripped)
    # Redaction markers are longer than the text they replace. A title they push
    # past the cap is refused like any other over-long title rather than cut,
    # so a stored title never holds a torn marker. The message names no part of
    # the title, so the redacted text is not echoed back.
    if len(scrubbed) > _MAX_RENAME_TITLE_CHARS:
        raise SessionControlError(
            f"title is longer than {_MAX_RENAME_TITLE_CHARS} characters once "
            "credential-shaped text in it is redacted; session not renamed",
            code="invalid_title",
            status=400,
        )
    return scrubbed


async def rename_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    title: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Rename *target*'s sidebar title, the way the sidebar's own rename does.

    The reach is :func:`stop_target`'s plus the caller itself. For a target that
    is not the caller, every rule in :func:`authorize_target` applies, including
    the ownership fence that keeps a crew member, a cron run, an owner-DM channel
    session and any agent-created session to the sessions it created. For the
    caller itself, ``allow_self`` waives the self-target refusal and, with it,
    that ownership fence, since a session naming itself acts on nothing but its
    own sidebar label. It waives nothing else: an ephemeral, app-scoped or
    channel-linked caller is still refused, and so is a cron run's own session,
    because the unattended-target refusal is checked before ``allow_self``.

    The rename is final (``_TITLE_ORIGIN_USER``), so the automatic titler stops
    refreshing the target, exactly as after a manual rename. It changes nothing
    else about the target and does not wait for a running turn.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    # Validated before any gate, as `set_model_target` does: a malformed title
    # needs no target lookup, and refusing it first keeps a bad argument from
    # reading as an access decision.
    clean_title = _clean_rename_title(title)
    # Same prewarm ordering as `stop_target`: nothing may suspend between the
    # gate and the write it authorizes.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the rename
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="rename",
        precomputed_ownership_fenced=caller_fenced,
        allow_self=True,
    )
    # `apply_manual_title` assigns every field before its first await, so the
    # title is on the slot before this coroutine can suspend.
    if not await apply_manual_title(state, slot, clean_title):
        _audit(
            caller_session_key=caller_session_key,
            operation="rename",
            slot_key=slot.key,
            outcome="error",
            detail={"chars": len(clean_title), "reason": "title_persist_failed"},
        )
        raise SessionControlError(
            f"{slot.key!r} now shows the new title, but it could not be saved yet; "
            "the session's next save writes it, and until then a restart brings back "
            "the previous title. Renaming again with the same title is safe",
            status=500,
            code="title_persist_failed",
        )
    _audit(
        caller_session_key=caller_session_key,
        operation="rename",
        slot_key=slot.key,
        outcome="allowed",
        detail={"chars": len(clean_title)},
    )
    # The slot's title, not this call's: a rename that overtook this one while it
    # saved has already replaced it, and the caller acts on what the reply names.
    return {"ok": True, "target": slot.key, "title": slot.title}


def _parse_session_color(color: str) -> int | None:
    """The palette index for a ``session_set_color`` value, or ``None`` to clear.

    ``"none"`` clears and ``"0"`` to ``"6"`` is one of the menu's swatches. Anything
    else is refused, including the menu's custom ``#rrggbb`` cell: grouping a
    fleet needs a few distinct marks, and the swatches follow the viewer's theme
    where a fixed hex does not.
    """
    if color in SESSION_COLOR_CHOICES:
        return None if color == "none" else int(color)
    raise SessionControlError(
        f'color must be a sidebar palette swatch index "0" to "{SESSION_PALETTE_SIZE - 1}", '
        'or "none" to clear; color not changed',
        code="invalid_color",
        status=400,
    )


async def set_color_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    color: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Set *target*'s sidebar color, the way the session color menu does.

    Reach is the sessions the caller created. Every rule in
    :func:`authorize_target` applies, including its ``self_target`` refusal, and
    then the creator fence runs for EVERY caller, as :func:`end_wait_target`'s
    does: the target must carry ``created_by`` equal to the caller's slot key. So an owner session with
    session control switched on for everyone still cannot recolor the person's
    own tabs. The write is the one a swatch
    click in ``PATCH /api/chat/slots/{slot}/color`` makes: the index is set and
    any custom hex the person picked is cleared, so exactly one color shows;
    ``"none"`` clears both. Metadata only.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    # Validated before any gate, as `set_model_target` does.
    color_index = _parse_session_color(color)
    # Same prewarm ordering as `stop_target`: nothing may suspend between the
    # gate and the write it authorizes.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the write
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="set_color",
        precomputed_ownership_fenced=caller_fenced,
    )
    caller_key = caller_slot_key(state, caller_session_key)
    if _created_by_other(slot, caller_key):
        deny = _deny_factory(
            caller_session_key=caller_session_key, operation="set_color", target=target
        )
        raise deny("this verb reaches only sessions the caller created", "not_creator")
    # The one color write the sidebar menu's PATCH route also makes. A swatch
    # always clears any custom hex, so exactly one color shows.
    write_slot_color(state, slot, color_index=color_index, color_hex=None)
    _audit(
        caller_session_key=caller_session_key,
        operation="set_color",
        slot_key=slot.key,
        outcome="allowed",
        detail={"color_index": color_index},
    )
    return {"ok": True, "target": slot.key, "color_index": color_index}


def _admit_generate_title(slot_key: str) -> None:
    """Spend *slot_key*'s one title generation for this window, or refuse."""
    if not allow_create(GENERATE_TITLE, slot_key):
        raise SessionControlError(
            f"{slot_key!r} had its title generated within the last "
            f"{int(WINDOW_SECS)} seconds; try again later",
            status=429,
            code="title_rate_limited",
        )


async def generate_title_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Regenerate *target*'s sidebar title, the way "Regenerate title" does.

    Runs the sidebar's own code (:func:`chat_title.generate_manual_title` and
    :func:`chat_title.apply_regenerated_title`), so the result is the one a
    person clicking it would get: one titling-model call, awaited before this
    returns, and a new name that replaces the current one (a hand-set title
    included) and stays "auto" so the background refresh may revise it.

    Reach is narrower than :func:`stop_target`'s. After every
    :func:`authorize_target` rule, the target must be a session the caller
    created (``_created_by``), for EVERY caller class: the verb spends a model
    call to relabel a session, and a session the person opened is theirs to
    name. The caller itself is refused too.

    One admitted call per target per ``create_rate_limit.WINDOW_SECS``, stamped
    before the model call, so a patrol loop or a concurrent retry cannot spend
    twice. A call refused before the stamp spends nothing.

    The model call suspends, so the whole gate runs again, synchronously, after
    it answers and before anything changes: a target that was closed, replaced
    under the same key, linked to a channel or otherwise put out of reach in the
    meantime keeps its title (``target_replaced`` or the gate's own refusal).
    From there the title is assigned with no suspension in between, the same
    guarantee :func:`rename_target` gives; the save that follows only makes an
    already-authorized change durable.

    ``caller_fenced`` has the meaning :func:`stop_target` documents.
    """
    # Same prewarm ordering as `stop_target`: nothing may suspend between the
    # gate and the write it authorizes.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the call
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    operation = "generate_title"
    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation=operation,
        precomputed_ownership_fenced=caller_fenced,
    )
    caller_key = caller_slot_key(state, caller_session_key)
    with _audit_denials(
        caller_session_key=caller_session_key, operation=operation, slot_key=slot.key
    ):
        if _created_by_other(slot, caller_key):
            raise SessionControlError(
                "session_generate_title only reaches sessions this session created",
                status=403,
                code="not_creator",
            )
        _admit_generate_title(slot.key)

    epoch = slot._title_epoch
    title = await generate_manual_title(state, slot)

    # Re-gate after the suspension. The creator rule was settled above and
    # ``_created_by`` belongs to this slot object, so the fence is passed as
    # settled and the switch read skipped: neither reads config on the loop.
    with _audit_denials(
        caller_session_key=caller_session_key, operation=operation, slot_key=slot.key
    ):
        if state._slots.get(slot.key) is not slot or slot.is_closing:
            raise SessionControlError(
                f"{slot.key!r} was closed while its title was being generated; "
                "nothing was changed",
                status=409,
                code="target_replaced",
            )
    authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=slot.key,
        operation=operation,
        skip_enabled_check=True,
        precomputed_ownership_fenced=True,
    )

    # A rename that landed during the model call outranks a generated name, as
    # in the sidebar; an empty result means the model had nothing better.
    if not title or slot._title_epoch != epoch:
        _audit(
            caller_session_key=caller_session_key,
            operation=operation,
            slot_key=slot.key,
            outcome="allowed",
            detail={"applied": False},
        )
        return {"ok": True, "target": slot.key, "title": slot.title, "changed": False}

    previous = slot.title

    # `apply_regenerated_title` assigns every field before its first await, so
    # the title is on the slot before this coroutine can suspend.
    # `still_current` is re-checked under the history write lock, so a slot
    # closed and replaced under the same key while the write was queued never
    # stamps its title over the successor's saved one.
    def _still_current() -> bool:
        return state._slots.get(slot.key) is slot

    if not await apply_regenerated_title(state, slot, title, still_current=_still_current):
        if not _still_current():
            _audit(
                caller_session_key=caller_session_key,
                operation=operation,
                slot_key=slot.key,
                outcome="denied",
                detail={"applied": False, "reason": "target_replaced"},
            )
            raise SessionControlError(
                f"{slot.key!r} was closed while its title was being saved; " "nothing was changed",
                status=409,
                code="target_replaced",
            )
        _audit(
            caller_session_key=caller_session_key,
            operation=operation,
            slot_key=slot.key,
            outcome="error",
            detail={"applied": True, "reason": "title_persist_failed"},
        )
        raise SessionControlError(
            f"{slot.key!r} now shows the new title, but it could not be saved yet; "
            "the session's next save writes it, and until then a restart brings back "
            "the previous title",
            status=500,
            code="title_persist_failed",
        )
    _audit(
        caller_session_key=caller_session_key,
        operation=operation,
        slot_key=slot.key,
        outcome="allowed",
        detail={"applied": True},
    )
    # The slot's title, not this call's: a rename that overtook this one while it
    # saved has already replaced it, and the caller acts on what the reply names.
    return {
        "ok": True,
        "target": slot.key,
        "title": slot.title,
        "changed": slot.title != previous,
    }
