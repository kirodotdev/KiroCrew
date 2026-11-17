"""``render(spec, platform)``: the one path from a card spec to a platform payload.

One pass over the spec, whose steps are, in effect, in this order:

1. **Structure.** Types, required parts, and counts (blocks, context items,
   buttons).
2. **Redact and defuse.** Every display string goes through
   :func:`~kiro_crew.messaging.renderer.display_safe_for` with
   :func:`~kiro_crew.platform.context.redact_via_context`, so the shared redactor
   (and a loaded companion's patterns) and the mention defuse run on every field.
   A ``Text``'s runs are joined first, since they read as one string. Slack
   ``mrkdwn`` is then escaped.
3. **Limits**, measured on the redacted, escaped string the platform receives,
   since a redaction placeholder can make text longer. Each breach is an
   :class:`~kiro_crew.messaging.cards.spec.Issue`. Under ``Fit.REFUSE`` it is
   raised in :class:`~kiro_crew.messaging.cards.spec.CardError` and nothing is
   returned; under ``Fit.KEEP`` it is logged and the value is sent unchanged.
4. **Emit.** Every string placed in the payload passes :meth:`_Pass.emit` in the
   form it ships, after every transformation, so what a reader sees is what was
   checked.

The renderer can emit only sections, context lines and button rows, with text
objects: nothing that names remote media or a URL. Every Slack send also passes
the client seam's ``blocks_request_remote_media`` refusal.

This module is neutral (``test_messaging_import_purity``), so it imports nothing
from a channel package. The Slack escape is the three replacements
``slack/format.escape_mrkdwn`` makes; a test holds them equal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from kiro_crew.messaging.cards.limits import SLACK_LIMITS, Limits
from kiro_crew.messaging.cards.spec import (
    Actions,
    Button,
    Card,
    CardError,
    Context,
    Fit,
    Issue,
    Section,
    Text,
)
from kiro_crew.messaging.renderer import display_safe_for
from kiro_crew.messaging.transport import TransportCapabilities
from kiro_crew.platform.context import redact_via_context

logger = logging.getLogger(__name__)


class Platform(Enum):
    SLACK = "slack"


@dataclass(frozen=True)
class Rendered:
    """The native payload: Slack ``blocks``."""

    blocks: list[dict[str, Any]]


#: Slack parses ``<!channel>`` and ``<@U…>``.
_CAPABILITIES = {Platform.SLACK: TransportCapabilities(mention_grammars=True)}
_DEFAULT_LIMITS = {Platform.SLACK: SLACK_LIMITS}
#: The redaction half of ``display_safe_for`` alone, for checking a string that
#: has already been defused.
_NO_MENTIONS = TransportCapabilities(mention_grammars=False)
_BUTTON_STYLES = ("default", "danger")


def _slack_bold(text: str) -> str:
    """``**bold**`` as Slack's ``*bold*``. A pair counts only when it closes on
    the same line with no ``*`` inside; anything else is left as written."""
    out, i = [], 0
    while (start := text.find("**", i)) >= 0:
        end = text.find("**", start + 2)
        inner = text[start + 2 : end] if end >= 0 else ""
        if not inner or "*" in inner or "\n" in inner:
            out.append(text[i : start + 1])
            i = start + 1
            continue
        out.append(f"{text[i:start]}*{inner}*")
        i = end + 2
    return "".join(out) + text[i:]


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _unescape(text: str) -> str:
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class _Pass:
    def __init__(self, platform: Platform, limits: Limits) -> None:
        self.caps = _CAPABILITIES[platform]
        self.limits = limits
        self.issues: list[Issue] = []
        self.refused = False

    def issue(
        self,
        path: str,
        code: str,
        message: str,
        limit: int | None = None,
        actual: int | None = None,
        *,
        refuse: bool = True,
    ) -> None:
        self.issues.append(Issue(path, code, limit, actual, message))
        if refuse:
            self.refused = True
        else:
            logger.info(
                "card field sent over its cap: %s %s limit=%s actual=%s", path, code, limit, actual
            )

    def fit(self, text: str, cap: int, fit: Fit, path: str) -> str:
        if len(text) > cap:
            if fit.mode == "keep":
                self.issue(path, "too_long", "Sent as given.", cap, len(text), refuse=False)
            else:
                self.issue(path, "too_long", f"Keep this to {cap} characters.", cap, len(text))
        return text

    def count(self, n: int, cap: int, path: str, what: str) -> None:
        if not n:
            self.issue(path, "empty", f"Needs at least one of its {what}.")
        elif n > cap:
            self.issue(path, "too_many", f"Use at most {cap} {what}.", cap, n)

    def shown(self, text: str, path: str) -> str:
        """*text* redacted and defused; refused when it then shows nothing."""
        out = display_safe_for(text, self.caps, redactor=redact_via_context)
        if not out.replace("\u200b", "").strip():
            self.issue(path, "empty", "This text shows nothing.")
        return out

    def emit(self, text: str, path: str, *, markup: bool) -> str:
        """The last check on every string placed in a payload, in the form it
        ships: redaction must find nothing left in what the reader sees.

        Whatever this finds was made after redaction (the escape, the bold
        translation), so the string is refused rather than shown. ``markup``
        strings are read with their three entities decoded, as Slack shows them.
        """
        reading = _unescape(text) if markup else text
        if display_safe_for(reading, _NO_MENTIONS, redactor=redact_via_context) != reading:
            self.issue(path, "unredacted", "This text shows a credential.")
        return text

    def ident(self, value: Any, path: str, cap: int, fit: Fit, *, required: bool) -> str:
        """Routing data the gateway owns: never shown, redacted or cut."""
        if not isinstance(value, str):
            self.issue(path, "bad_type", "This must be text.")
            return ""
        if required and not value:
            self.issue(path, "empty", "This id is required.")
        return self.fit(value, cap, fit, path)

    def mrkdwn(self, text: Any, path: str, cap: int) -> dict[str, str]:
        """*text* as a Slack ``mrkdwn`` text object, fitted to *cap*."""
        if not isinstance(text, Text) or not isinstance(text.fit, Fit):
            self.issue(path, "bad_type", "This must be a Text.")
            return {"type": "mrkdwn", "text": ""}
        if not isinstance(text.runs, tuple) or not all(isinstance(r, str) for r in text.runs):
            self.issue(path, "bad_type", "A Text's runs are strings.")
            return {"type": "mrkdwn", "text": ""}
        safe = _slack_bold(_escape(self.shown("".join(text.runs), path)))
        out = self.emit(self.fit(safe, cap, text.fit, path), path, markup=True)
        return {"type": "mrkdwn", "text": out}


def _slack_button(p: _Pass, b: Any, path: str) -> dict[str, Any]:
    if not isinstance(b, Button) or not isinstance(b.fit, Fit):
        p.issue(path, "bad_type", "This must be a Button.")
        return {}
    lim = p.limits
    if b.style not in _BUTTON_STYLES:
        p.issue(f"{path}.style", "bad_type", "Style is default or danger.")
    if not isinstance(b.label, str):
        p.issue(f"{path}.label", "bad_type", "This must be text.")
        label = ""
    else:
        label = p.fit(p.shown(b.label, f"{path}.label"), lim.button_label, b.fit, f"{path}.label")
    out: dict[str, Any] = {
        "type": "button",
        "action_id": p.ident(b.action_id, f"{path}.action_id", lim.action_id, b.fit, required=True),
        "text": {"type": "plain_text", "text": p.emit(label, f"{path}.label", markup=False)},
    }
    if b.value is not None:
        out["value"] = p.ident(b.value, f"{path}.value", lim.button_value, b.fit, required=False)
    if b.style != "default":
        out["style"] = b.style
    return out


def _slack_block(p: _Pass, block: Any, path: str) -> dict[str, Any]:
    lim = p.limits
    if isinstance(block, Section):
        return {"type": "section", "text": p.mrkdwn(block.text, f"{path}.text", lim.section_text)}
    if isinstance(block, Context):
        p.count(len(block.items), lim.context_items, f"{path}.items", "context items")
        return {
            "type": "context",
            "elements": [
                p.mrkdwn(t, f"{path}.items[{i}]", lim.section_text)
                for i, t in enumerate(block.items)
            ],
        }
    if isinstance(block, Actions) and isinstance(block.fit, Fit):
        p.count(len(block.buttons), lim.buttons, f"{path}.buttons", "buttons")
        out: dict[str, Any] = {"type": "actions"}
        if block.id:
            out["block_id"] = p.ident(
                block.id, f"{path}.id", lim.block_id, block.fit, required=False
            )
        out["elements"] = [
            _slack_button(p, b, f"{path}.buttons[{i}]") for i, b in enumerate(block.buttons)
        ]
        return out
    p.issue(path, "bad_type", "Not a block this card can show.")
    return {}


def render(spec: Card, platform: Platform, limits: Limits | None = None) -> Rendered:
    """Render *spec* for *platform*. Raises :class:`CardError` and returns nothing
    when any check fails under ``Fit.REFUSE``."""
    if not isinstance(spec, Card) or not isinstance(spec.body, tuple):
        raise CardError([Issue("card", "bad_type", None, None, "This must be a Card.")])
    lim = limits or _DEFAULT_LIMITS[platform]
    p = _Pass(platform, lim)
    p.count(len(spec.body), lim.blocks, "body", "blocks")
    blocks = [_slack_block(p, b, f"body[{i}]") for i, b in enumerate(spec.body)]
    if p.refused:
        raise CardError(list(p.issues))
    return Rendered(blocks=blocks)
