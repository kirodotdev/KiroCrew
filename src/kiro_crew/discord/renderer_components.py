"""Discord message components: the button rows a turn and the dispatcher post.

The button styles, Discord's layout limits (five buttons to a row, five rows to a
message, an 80-character label) and the two row builders: ``build_option_components``
for an answer's ``[OPTIONS:]`` choices and ``build_model_components`` for the ``!model``
picker. The renderer, the dispatcher's model picker and its spawn-approval prompt all
build their buttons from here, so new component payloads go here.
:mod:`kiro_crew.discord.renderer` imports these and stays their import path and patch
surface.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Sequence

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
