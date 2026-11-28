"""Discord message components: the button rows a turn and the dispatcher post.

The button styles, Discord's layout limits (five buttons to a row, five rows to a
message, an 80-character label) and the row builders: ``build_option_rows`` for an
answer's ``[OPTIONS:]`` choices (``build_option_components`` buttons plus string
selects, with the registry of the select prompts a press may still answer),
``build_model_components`` for the ``!model``
picker and ``build_stop_components`` for the Stop button on a streaming reply, with the
registry of the turns a Stop button may still cancel. The renderer, the dispatcher's
model picker and its spawn-approval prompt all
build their buttons from here, so new component payloads go here.
:mod:`kiro_crew.discord.renderer` imports these and stays their import path and patch
surface.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

# Button style constants (Discord component styles).
_STYLE_PRIMARY = 1
_STYLE_SECONDARY = 2
_STYLE_SUCCESS = 3
_STYLE_DANGER = 4

# Discord component layout limits: an action row holds at most 5 buttons, and a
# legacy (non-Components-V2) message holds at most 5 action rows.
_BUTTONS_PER_ROW = 5
_MAX_ACTION_ROWS = 5
#: Platform hard-limit backstop for any button set. A caller's own cap decides
#: how many choices to offer; this only guarantees the payload Discord accepts.
_MAX_BUTTONS = _BUTTONS_PER_ROW * _MAX_ACTION_ROWS
#: Component-spec ceiling on a button label.
_BUTTON_LABEL_CHARS = 80

#: Full text of every ``[OPTIONS:]`` choice whose button label was cut, keyed by
#: ``(origin_tag, choice_ref)``. A press reads the label Discord sends back, and a
#: cut label is not the choice the agent offered, so the press answers with the
#: text kept here. Process-local and bounded (oldest dropped first): after a
#: restart, or once evicted, a press on a cut button asks the person to type it.
_FULL_CHOICES: OrderedDict[tuple[str, str], str] = OrderedDict()
_FULL_CHOICES_MAX = 1024
_FULL_CHOICES_LOCK = threading.Lock()
#: Longest choice text kept, so the map is bounded in characters as well as in
#: entries (at most 1024 x 1000). A longer choice is not kept, and its press asks
#: the person to type it. 1000 also keeps the press echo (``> `` + the choice)
#: inside one 2000-character Discord message.
_FULL_CHOICE_MAX_CHARS = 1000


def _choice_ref(index: int, option: str) -> str:
    """The ``custom_id`` middle part: the index, plus a digest when the label is cut.

    The digest names the full text, so two long choices that share their first 80
    characters still answer with their own text. ``:`` never appears in it, so the
    dispatcher's ``split(":", 2)`` still reads the origin tag as the third part.
    """
    if len(option) <= _BUTTON_LABEL_CHARS:
        return str(index)
    digest = hashlib.sha256(option.encode("utf-8")).hexdigest()[:12]
    return f"{index}~{digest}"


def _remember_full_choice(origin_tag: str, choice_ref: str, option: str) -> None:
    if len(option) > _FULL_CHOICE_MAX_CHARS:
        return
    with _FULL_CHOICES_LOCK:
        key = (origin_tag, choice_ref)
        _FULL_CHOICES[key] = option
        _FULL_CHOICES.move_to_end(key)
        while len(_FULL_CHOICES) > _FULL_CHOICES_MAX:
            _FULL_CHOICES.popitem(last=False)


def full_choice_text(origin_tag: str, choice_ref: str, shown_label: str) -> str:
    """The choice a press on ``opt:<choice_ref>:<origin_tag>`` answers with.

    A short choice answers with its label as Discord sent it back. A cut choice
    (a ``choice_ref`` with a digest) answers with the full text kept here, or with
    ``""`` when it is not kept (a restart, or evicted): its label is not the
    choice, so the caller asks the person to type it rather than send the cut text.
    """
    if "~" not in choice_ref:
        return shown_label
    with _FULL_CHOICES_LOCK:
        return _FULL_CHOICES.get((origin_tag, choice_ref), "")


def build_option_components(options: list[str], origin_tag: str = "") -> list[dict] | None:
    """Build Discord button action rows from ``[OPTIONS:]`` labels.

    ``custom_id`` is ``opt:<ref>:<origin_tag>`` (``opt:<ref>`` when no tag is given --
    the pre-provenance legacy shape) -- Discord caps it at 100 chars, which the
    12-hex tag fits comfortably. ``<ref>`` is the index, plus a digest of the full
    text when the label is cut (see :func:`_choice_ref`). Labels cap at 80 chars per
    the component spec; a press recovers the choice from the button text, or from
    :func:`full_choice_text` when the label was cut. The ``max_buttons`` cap is
    applied UPSTREAM via ``apply_options_cap`` (overflow degrades to numbered
    text); the slice below is the platform hard-limit backstop only.
    """
    if not options:
        return None
    suffix = f":{origin_tag}" if origin_tag else ""
    rows: list[dict] = []
    row: list[dict] = []
    for i, opt in enumerate(options[:_MAX_BUTTONS]):
        ref = _choice_ref(i, opt)
        if origin_tag and ref != str(i):
            _remember_full_choice(origin_tag, ref, opt)
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_SECONDARY,
                "label": opt[:_BUTTON_LABEL_CHARS],
                "custom_id": f"opt:{ref}{suffix}",
            }
        )
        if len(row) == _BUTTONS_PER_ROW:
            rows.append({"type": 1, "components": row})  # action row
            row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


#: Select-menu custom_id prefix: ``sel:<nonce>:<menu>``. Distinct from ``opt:``,
#: ``s:``, ``a:``, ``m:`` and ``x:`` so the dispatcher's prefix routing cannot read
#: one as another.
SELECT_PREFIX = "sel:"
#: Component-spec ceilings on a string select: 25 options, a 100-character option
#: label and a 150-character placeholder.
_SELECT_OPTIONS_MAX = 25
_SELECT_LABEL_CHARS = 100
#: Up to this many choices show as buttons (four rows) with a "Choose several..."
#: select in the fifth row, the way Slack's card ends in its overflow menu. A
#: longer list shows as selects only, one per row.
_BUTTONS_WITH_SELECT = _BUTTONS_PER_ROW * (_MAX_ACTION_ROWS - 1)
#: TOTAL choices one message can offer: five selects of 25 each.
MAX_OPTION_CHOICES = _SELECT_OPTIONS_MAX * _MAX_ACTION_ROWS


@dataclass(frozen=True)
class _OptionSelect:
    """One posted set of option selects: the session tag, every choice offered
    (full text) and each menu's ``[start, stop)`` slice of them."""

    origin_tag: str
    choices: tuple[str, ...]
    menus: tuple[tuple[int, int], ...]


#: The select prompts a press may still answer: nonce -> what was offered. A press
#: answers with the text kept here, never with anything the client sends back, and
#: the entry is taken by the first valid press, so a prompt answers once. Posting
#: a newer prompt for the same session retires the older one, and so does the
#: start of the session's next turn (:func:`retire_option_selects`): a select from
#: an older prompt, or one Discord replays after a restart (when this map is
#: empty), names no entry and answers nothing. Bounded like ``_STOP_TURNS``.
#: process-cache-ok: keyed by a random per-prompt nonce, so no later test can read
#: an entry an earlier one left; the cap only ever evicts the oldest.
_OPTION_SELECTS: OrderedDict[str, _OptionSelect] = OrderedDict()
_OPTION_SELECTS_MAX = 256
_OPTION_SELECTS_LOCK = threading.Lock()


def _register_option_select(
    origin_tag: str, choices: tuple[str, ...], menus: tuple[tuple[int, int], ...]
) -> str:
    nonce = secrets.token_hex(8)
    # Bounded in characters as well as entries, like ``_FULL_CHOICES``: a choice
    # past the ceiling is not kept, and a press picking it asks to type it.
    kept = tuple(c if len(c) <= _FULL_CHOICE_MAX_CHARS else "" for c in choices)
    with _OPTION_SELECTS_LOCK:
        for stale in [k for k, v in _OPTION_SELECTS.items() if v.origin_tag == origin_tag]:
            del _OPTION_SELECTS[stale]
        _OPTION_SELECTS[nonce] = _OptionSelect(origin_tag, kept, menus)
        while len(_OPTION_SELECTS) > _OPTION_SELECTS_MAX:
            _OPTION_SELECTS.popitem(last=False)
    return nonce


def retire_option_selects(origin_tag: str) -> None:
    """Retire every select prompt the session tagged *origin_tag* posted."""
    if not origin_tag:
        return
    with _OPTION_SELECTS_LOCK:
        for stale in [k for k, v in _OPTION_SELECTS.items() if v.origin_tag == origin_tag]:
            del _OPTION_SELECTS[stale]


#: :func:`take_option_select` outcomes.
SELECT_ANSWERED = "answered"
SELECT_STALE = "stale"
SELECT_INVALID = "invalid"


def take_option_select(custom_id: str, values: Sequence[str]) -> tuple[str, str, list[str]]:
    """Check a select press against what was offered and, when it fits, consume it.

    Returns ``(outcome, origin_tag, choices)``. ``SELECT_STALE``: the prompt is not
    live (already answered, superseded, from before a restart, or never posted).
    ``SELECT_INVALID``: the prompt is live but *values* do not fit the pressed menu
    (an index it did not offer, a repeat, or fewer than one or more than the menu
    holds); the prompt stays live. ``SELECT_ANSWERED``: the prompt is taken, and
    ``choices`` is the full text of each pick in the order offered. The check and
    the take run under one lock, so two presses cannot both answer.
    """
    nonce, _, menu_ref = custom_id[len(SELECT_PREFIX) :].partition(":")
    with _OPTION_SELECTS_LOCK:
        entry = _OPTION_SELECTS.get(nonce)
        if entry is None:
            return SELECT_STALE, "", []
        if not (menu_ref.isascii() and menu_ref.isdigit()) or int(menu_ref) >= len(entry.menus):
            return SELECT_INVALID, entry.origin_tag, []
        start, stop = entry.menus[int(menu_ref)]
        picked: set[int] = set()
        for value in values:
            if not (value.isascii() and value.isdigit()) or len(value) > 3:
                return SELECT_INVALID, entry.origin_tag, []
            index = int(value)
            if not start <= index < stop or index in picked:
                return SELECT_INVALID, entry.origin_tag, []
            picked.add(index)
        if not 1 <= len(picked) <= stop - start:
            return SELECT_INVALID, entry.origin_tag, []
        if not all(entry.choices[i] for i in picked):
            # A pick whose text was too long to keep: never send its cut label.
            return SELECT_INVALID, entry.origin_tag, []
        del _OPTION_SELECTS[nonce]
    return SELECT_ANSWERED, entry.origin_tag, [entry.choices[i] for i in sorted(picked)]


def _select_label(option: str) -> str:
    if len(option) <= _SELECT_LABEL_CHARS:
        return option or "…"
    return option[: _SELECT_LABEL_CHARS - 1] + "…"


def build_option_rows(options: list[str], origin_tag: str = "") -> list[dict] | None:
    """Action rows for an answer's ``[OPTIONS:]`` choices, buttons and selects.

    Up to 20 choices: one button each (:func:`build_option_components`), and, for
    two or more, a "Choose several..." select in the last row that sends every pick
    together, as Slack's card does. More than 20: one select per row, 25 choices
    each, up to :data:`MAX_OPTION_CHOICES`; one or several picks in a menu send
    together. The caller caps the list upstream (``apply_options_cap``), and the
    slice here is the platform backstop only.

    A select's ``custom_id`` is ``sel:<nonce>:<menu>`` and each option's ``value``
    is the choice's index. The nonce names the exact list kept server-side, so a
    press answers with the offered text and is checked against it.
    """
    if not options:
        return None
    options = options[:MAX_OPTION_CHOICES]
    count = len(options)
    if count <= _BUTTONS_WITH_SELECT:
        rows = build_option_components(options, origin_tag) or []
        if count < 2:
            # Still a newer prompt: it supersedes any menu this session posted.
            retire_option_selects(origin_tag)
            return rows
        menus: tuple[tuple[int, int], ...] = ((0, count),)
        placeholders = ["Choose several…"]
    else:
        # shortcut: picks in two different menus cannot go together, since each
        # menu answers on its own; upgrade to a modal if that is ever asked for.
        rows = []
        menus = tuple(
            (start, min(start + _SELECT_OPTIONS_MAX, count))
            for start in range(0, count, _SELECT_OPTIONS_MAX)
        )
        placeholders = [f"Choose from {start + 1}–{stop}" for start, stop in menus]
    nonce = _register_option_select(origin_tag, tuple(options), menus)
    for menu, ((start, stop), placeholder) in enumerate(zip(menus, placeholders)):
        rows.append(
            {
                "type": 1,  # action row
                "components": [
                    {
                        "type": 3,  # string select
                        "custom_id": f"{SELECT_PREFIX}{nonce}:{menu}",
                        "placeholder": placeholder,
                        "min_values": 1,
                        "max_values": stop - start,
                        "options": [
                            {"label": _select_label(options[i]), "value": str(i)}
                            for i in range(start, stop)
                        ],
                    }
                ],
            }
        )
    return rows


def build_model_components(choices: Sequence[tuple[str, str]], current: str) -> list[dict] | None:
    """Build the ``!model`` button rows from ``(model_id, label)`` choices.

    ``custom_id`` is the INDEX only (``m:<i>``), never the model id: Discord caps
    a custom_id at 100 characters and replays old component ids indefinitely, so
    an id embedded here would both risk the cap and outlive the advertised set it
    came from. The dispatcher's picker registry resolves the index back against
    the exact list it posted, and refuses once that list has expired.

    The current pick is marked with a bullet and rendered in the primary style,
    so the active model is readable without pressing anything.
    """
    if not choices:
        return None
    rows: list[dict] = []
    row: list[dict] = []
    for i, (model_id, label) in enumerate(choices[:_MAX_BUTTONS]):
        picked = model_id == current
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_PRIMARY if picked else _STYLE_SECONDARY,
                "label": (f"• {label}" if picked else label)[:_BUTTON_LABEL_CHARS],
                "custom_id": f"m:{i}",
            }
        )
        if len(row) == _BUTTONS_PER_ROW:
            rows.append({"type": 1, "components": row})  # action row
            row = []
    if row:
        rows.append({"type": 1, "components": row})
    return rows


#: Stop button custom_id prefix: ``x:<nonce>``. Distinct from ``s:``, ``a:``, ``m:``
#: and ``opt:`` so the dispatcher's prefix routing cannot read one as another.
STOP_PREFIX = "x:"

#: The turns a Stop button may still cancel: nonce -> (session key, channel id, the
#: task holding the turn). One nonce per turn, minted with the turn's first streamed
#: bubble and retired when the turn finalizes, BEFORE its session is released.
#: Discord replays old buttons indefinitely, so a Stop left on a bubble whose
#: retirement edit failed (or one from before a restart, when this map is empty)
#: names no entry here and stops nothing. Bounded like ``_FULL_CHOICES``: a renderer
#: that is never closed cannot grow it.
#: process-cache-ok: keyed by a random per-turn nonce, so no later test can read an
#: entry an earlier one left; the cap only ever evicts the oldest.
_STOP_TURNS: OrderedDict[str, tuple[str, str, object]] = OrderedDict()
_STOP_TURNS_MAX = 1024
_STOP_TURNS_LOCK = threading.Lock()


def register_stop_turn(session_key: str, channel_id: str, owner: object) -> str:
    """Mint the nonce a turn's Stop button carries.

    Bound to the turn's session, its channel and *owner*, the task holding the
    turn, so a press can check that the session is still held by that turn.
    """
    nonce = secrets.token_hex(8)
    with _STOP_TURNS_LOCK:
        _STOP_TURNS[nonce] = (session_key, channel_id, owner)
        while len(_STOP_TURNS) > _STOP_TURNS_MAX:
            _STOP_TURNS.popitem(last=False)
    return nonce


def retire_stop_turn(nonce: str) -> None:
    """Retire a turn's Stop nonce; every button carrying it now stops nothing."""
    if nonce:
        with _STOP_TURNS_LOCK:
            _STOP_TURNS.pop(nonce, None)


def stop_turn_target(nonce: str) -> tuple[str, str, object] | None:
    """``(session key, channel id, owner task)`` of the turn *nonce* names, or ``None``."""
    with _STOP_TURNS_LOCK:
        return _STOP_TURNS.get(nonce)


def build_stop_components(nonce: str) -> list[dict]:
    """One action row holding the Stop button for the turn *nonce* names."""
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": _STYLE_DANGER,
                    "label": "⏹ Stop",
                    "custom_id": f"{STOP_PREFIX}{nonce}",
                }
            ],
        }
    ]
