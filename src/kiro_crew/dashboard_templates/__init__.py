"""Dashboard templates: one authored page, one typed contract, one provider.

A dashboard card is two halves. The ``html`` half is inert layout the host
sanitizes and renders; the ``data`` half is a flat map of text the host binds into
it by ``data-dashboard-field``. Nothing about that binding is checked by a type
checker, because one half is markup, so a template that reads a field its provider
never fills renders an empty cell and nobody is accountable for it.

This package is where the two halves are made to agree. A template is FOUR files
and one registration line:

* ``<slug>.html`` -- the page, with one ``data-dashboard-field`` per value.
* ``<slug>_contract.py`` -- the :class:`~typing.TypedDict` naming exactly those
  fields, plus the judgment type a publisher may write and the ``CONTRACT_VERSION``
  a reader branches on.
* ``<slug>_provider.py`` -- ``build_<slug>``, whose RETURN TYPE is the contract, so
  mypy refuses a missing or misspelled key at build time.
* ``test/test_dashboard_template_<slug>.py`` -- the parity gate, which is what
  covers the half mypy cannot see.

All of it is DEV TIME. A template lands through a pull request, is checked by
``mypy`` (blocking) and by its parity test, and only then ships. The gateway never
runs agent-authored fold code and never evaluates an agent-authored expression: at
run time a publisher supplies the judgment fields only, and every number on the
page is derived here from a fold the product already keeps.

Three invariants hold for every template in the registry, and each is a test rather
than a convention:

1. **Parity.** The set of ``data-dashboard-field`` names in the html equals the set
   of keys in the contract, both directions. A field the html reads and the
   contract omits is an empty cell; a key the contract declares and the html never
   reads is a provider deriving something nobody sees.
2. **Every number carries its denominator.** A count alone ("7 failed") tells a
   reader nothing about the size of the thing it counts. A numeric field reaches the
   card as ``N/M``, or it declares its denominator as a sibling field.
3. **A value nobody supplied says so.** :data:`UNSAID` is a REQUIRED value, not an
   absent key, and :func:`card_data` turns it into the words :data:`NOT_SAID`. The
   host renders a key it was not given as the empty string, which on a page of
   numbers is indistinguishable from a zero -- so the gap is named instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Final

__all__ = [
    "MAX_CARD_DATA_BYTES",
    "MAX_CARD_FIELDS",
    "NOT_SAID",
    "UNSAID",
    "CardData",
    "TemplateSpec",
    "Unsaid",
    "card_data",
    "field_name_ok",
    "fraction",
    "gate_action",
    "gate_text",
    "read_int",
    "read_text",
]


# The sentinel


class Unsaid(Enum):
    """The type of "nobody supplied this". A required value, never an absent key.

    An ENUM, not a string sentinel, and that is the whole reason this type exists as
    its own thing. A ``Literal["__unsaid__"]`` is forgeable: a fold value or a
    publisher's sentence that happens to BE that text is indistinguishable from the
    gap, so :func:`card_data` would rewrite real content as "not said" -- and a
    publisher could print what looks like a system marker onto a status page. No
    writer can produce this member, because it is not text at all.

    Single-membered on purpose: a type checker narrows ``str | Unsaid`` to ``str``
    once the member is ruled out, which is what lets every reader below say what it
    means without a cast.
    """

    UNSAID = "unsaid"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNSAID"


UNSAID: Final = Unsaid.UNSAID
"""Write this explicitly. It cannot arrive from a lookup that returned nothing."""

NOT_SAID: Final[str] = "not said"
"""What :data:`UNSAID` reads as on the page.

Words, not a blank and not a zero. The host binds a field it was given by setting
``textContent``, and a field it was NOT given to the empty string -- so on a card of
counts an unsupplied value and a real zero look the same, which is the one reading a
status page must never produce.
"""

CardData = dict[str, str]
"""The ``data`` half of a card: flat, text only, bound by ``data-dashboard-field``."""


# The host's own limits, restated
#
# RESTATED, not imported: the owner, ``kiro_crew.dashboard.dynamic_cards.normalize_card``,
# exports only ``MAX_DATA_BYTES``; the field cap is an inline literal there and the
# binding attribute lives in the browser half
# (``website/src/pages/chat/command-center/dashboardDocument.ts``).
# ``TestTheRestatedHostLimits`` checks every value against the host on each run. The host
# drops an over-cap card WHOLE, so a page one field too big disappears.

MAX_CARD_FIELDS: Final[int] = 24
"""Fields one card's ``data`` may carry. A card over the cap is refused entire."""

MAX_CARD_DATA_BYTES: Final[int] = 4096
"""Bytes of keys plus values one card's ``data`` may carry, UTF-8."""

#: A binding name the host accepts. Mirrors its normalizer's own pattern, so a name
#: this package emits cannot be one the host silently drops.
_FIELD_NAME: Final[re.Pattern[str]] = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,47}\Z")


def field_name_ok(name: str) -> bool:
    """Whether *name* is a binding the host will accept."""
    return _FIELD_NAME.fullmatch(name) is not None


# Reading a fold, without inventing a zero
#
# A fold's rendered value is a plain JSON object. A provider reading it with
# ``value.get(key, 0)`` converts "this fold does not carry that" into a number, which
# is invariant 3 broken at the first line of the provider rather than at the page. So
# a provider reads through these two, which answer UNSAID for absent, wrong-typed and
# unparsable alike.


def read_text(view: Mapping[str, Any], key: str) -> str | Unsaid:
    """*view*'s *key* as non-empty text, else :data:`UNSAID`."""
    value = view.get(key)
    if not isinstance(value, str):
        return UNSAID
    stripped = value.strip()
    return stripped if stripped else UNSAID


def read_int(view: Mapping[str, Any], key: str) -> int | Unsaid:
    """*view*'s *key* as an int, else :data:`UNSAID`.

    ``bool`` is refused: it passes ``isinstance(x, int)`` and would render ``True``
    as ``1``, turning a flag into a count.
    """
    value = view.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return UNSAID
    return value


def gate_text(value: str | Unsaid) -> str | Unsaid:
    """A sentence, or :data:`UNSAID`. Blank text is a gap, not an empty sentence.

    Lives HERE rather than being emitted into each provider: a publisher gate must be
    identical across templates, and a copy per template would let a fix reach only the
    templates written after it.
    """
    if isinstance(value, Unsaid):
        return UNSAID
    text = value.strip()
    return text if text else UNSAID


def gate_action(value: str | Unsaid) -> str | Unsaid:
    """An action, or :data:`UNSAID`.

    A one-token value is refused. The field says what a person must DO, and a name or a
    session id (``Raymond``, ``chat-2176``) is not an action. Phrase SHAPE only, with no
    length floor: a floor would not stop a two-word name, and it would suppress short
    real actions like ``fix it``.
    """
    if isinstance(value, Unsaid):
        return UNSAID
    parts = value.split()
    if len(parts) < 2:
        return UNSAID
    return " ".join(parts)


def fraction(part: int | Unsaid, whole: int | Unsaid) -> str | Unsaid:
    """``"N/M"``, or :data:`UNSAID` when either side is unknown.

    Invariant 2 in one function. A count whose total is unknown is not rendered as
    the count alone, because "7" on a status card reads as complete information and
    is not: the reader cannot tell 7 of 7 from 7 of 700.
    """
    if isinstance(part, Unsaid) or isinstance(whole, Unsaid):
        return UNSAID
    return f"{part}/{whole}"


# The one exit to the card


def card_data(payload: Mapping[str, Any]) -> CardData:
    """*payload* as the card's ``data``: every value text, every gap named.

    ONE exit, so the sentinel cannot reach the page and a gap cannot reach it as a
    blank. A value that is :data:`UNSAID` becomes :data:`NOT_SAID`; anything else is
    stringified, which for this package's contracts means an ``int`` or a ``str``
    because a contract declares nothing else.

    Raises :class:`ValueError` on a payload the host would refuse -- too many fields,
    too many bytes, a name it would not bind. Loudly here rather than silently there:
    the host drops an over-cap card whole, so a page that outgrew the cap would simply
    stop appearing, with nothing red anywhere.
    """
    out: CardData = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not field_name_ok(key):
            raise ValueError(f"{key!r} is not a field name the host will bind")
        # ``isinstance``, not a text comparison: the sentinel is an enum member, so no
        # value a fold or a publisher can supply reaches this branch by accident.
        out[key] = NOT_SAID if isinstance(value, Unsaid) else str(value)
    if len(out) > MAX_CARD_FIELDS:
        raise ValueError(
            f"{len(out)} fields exceeds the host's cap of {MAX_CARD_FIELDS}; "
            "the host refuses an over-cap card whole, so the page would vanish"
        )
    size = sum(len(k.encode("utf-8")) + len(v.encode("utf-8")) for k, v in out.items())
    if size > MAX_CARD_DATA_BYTES:
        raise ValueError(
            f"{size} bytes of card data exceeds the host's cap of {MAX_CARD_DATA_BYTES}"
        )
    return out


# What a registration is


@dataclass(frozen=True)
class TemplateSpec:
    """One template: its page, its contract, its provider, and the fold behind it.

    ``fold`` is the name of the projection the numbers come FROM, and it must be one
    of the folds the product already keeps. That is the whole point of naming it here:
    a template whose numbers have no fold behind them is a template whose numbers were
    typed by somebody, which is the failure this package exists to remove.
    """

    slug: str
    #: The contract ``TypedDict``. Typed loosely because a registry holds many
    #: different ones; each provider's own signature is where mypy does the work.
    contract: type
    #: ``build_<slug>``. Its return annotation is asserted to BE ``contract``.
    provider: Callable[..., Any]
    #: The projection the derived values come from, e.g. ``"work"``.
    fold: str
    #: ``CONTRACT_VERSION`` from the contract module.
    version: int
