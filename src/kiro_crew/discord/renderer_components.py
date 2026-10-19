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


def build_option_components(options: list[str], origin_tag: str = "") -> list[dict] | None:
    """Build Discord button action rows from ``[OPTIONS:]`` labels.

    ``custom_id`` is ``opt:<i>:<origin_tag>`` (``opt:<i>`` when no tag is given --
    the pre-provenance legacy shape) -- Discord caps it at 100 chars, which the
    12-hex tag fits comfortably, and the label is recovered from the button text
    at interaction time. Labels cap at 80 chars per the component spec. The
    ``max_buttons`` cap is applied UPSTREAM via ``apply_options_cap`` (overflow
    degrades to numbered text); the slice below is the platform hard-limit
    backstop only.
    """
    if not options:
        return None
    suffix = f":{origin_tag}" if origin_tag else ""
    rows: list[dict] = []
    row: list[dict] = []
    for i, opt in enumerate(options[:_MAX_BUTTONS]):
        row.append(
            {
                "type": 2,  # button
                "style": _STYLE_SECONDARY,
                "label": opt[:_BUTTON_LABEL_CHARS],
                "custom_id": f"opt:{i}{suffix}",
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
