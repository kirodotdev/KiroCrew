"""The typed card spec: what a gateway builder says a card holds.

Every class is a frozen dataclass, so a rebuild is ``dataclasses.replace`` on a
stored spec rather than an edit of posted JSON. :func:`kiro_crew.messaging.cards.
render.render` turns a spec into a platform payload; nothing here knows a platform.

The element set holds what the cards built from it use today: sections, context
lines and button rows. It has no image, video, link preview or URL of any kind.
The RFC's other elements and fits land with the first card that uses each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, Union


@dataclass(frozen=True)
class Fit:
    """What a field does over its platform cap. Every fit is chosen in code.

    * ``REFUSE``: the cap breach is an :class:`Issue` and ``render`` raises
      :class:`CardError`; nothing posts. The default for every field.
    * ``KEEP``: sent unchanged, so the platform answers an over-cap value exactly
      as it does for the hand-built payload, and the issue is logged. For routing
      data a migrated builder cannot cut (an action id, a block id, a button
      value).
    """

    mode: Literal["refuse", "keep"]

    REFUSE: ClassVar[Fit]
    KEEP: ClassVar[Fit]


Fit.REFUSE = Fit("refuse")
Fit.KEEP = Fit("keep")


@dataclass(frozen=True)
class Text:
    """Display text in a small markdown subset the renderer translates
    (``**bold**`` and ``_italic_``). The runs read as one string and are redacted
    as one."""

    runs: tuple[str, ...]
    fit: Fit = Fit.REFUSE


@dataclass(frozen=True)
class Section:
    text: Text


@dataclass(frozen=True)
class Context:
    """Small, secondary lines."""

    items: tuple[Text, ...]


@dataclass(frozen=True)
class Button:
    """A press. ``action_id`` routes the click and is gateway-owned; ``value`` is a
    lookup key into state the process already holds, never authority, and is left
    off the payload when None. ``fit`` covers the label, the action id and the
    value."""

    label: str
    action_id: str
    value: str | None = None
    style: Literal["default", "danger"] = "default"
    fit: Fit = Fit.REFUSE


@dataclass(frozen=True)
class Actions:
    """A row of buttons. ``id`` is Slack's ``block_id``, left off when empty;
    ``fit`` covers it, as :class:`Button` does its ids."""

    buttons: tuple[Button, ...]
    id: str = ""
    fit: Fit = Fit.REFUSE


Block = Union[Section, Context, Actions]


@dataclass(frozen=True)
class Card:
    body: tuple[Block, ...]


@dataclass(frozen=True)
class Issue:
    """One reason a spec does not fit, in a form a model or a log reader can act on."""

    path: str  # "body[1].buttons[0].label"
    code: str  # "too_long", "too_many", "empty", "bad_type", "unredacted"
    limit: int | None
    actual: int | None
    message: str


class CardError(ValueError):
    """``render`` refused a spec; ``issues`` says why, one entry per breach."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.path}: {i.code}" for i in issues) or "card refused")
