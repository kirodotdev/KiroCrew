"""Adaptive Card builders — Webex's analogue of Slack's Block Kit.

Webex renders an Adaptive Card sent as a message ``attachments[]`` entry, and a
press comes back as an ``attachmentActions`` activity whose ``inputs`` map merges
the card's collected input values with the pressed action's own ``data``. That
``data`` is therefore the action-id round trip: the same mechanism as a Slack
``action_id``, spelled differently.

Two things shape everything here:

* **A card is an ADDITION, never the only affordance.** Webex requires the
  message to also carry ``text``/``markdown`` as a fallback for clients that
  cannot render cards, and the inbound half rides the undocumented device
  websocket — so a card whose press never arrives must still leave the user a way
  to answer. Every builder below therefore pairs with a text prompt that works on
  its own, and the dispatcher accepts either.
* **A card cannot be retired in place.** Webex refuses to edit a message carrying
  an attachment, so a resolved prompt is answered with a NEW message rather than
  by rewriting the card. The card's own nonce is what stops a stale press being
  honoured after that.

Version 1.3 is the ceiling Webex documents, so nothing here uses a later
feature. Only ONE card is allowed per message.
"""

from __future__ import annotations

import secrets
from typing import Any

#: The attachment content type Webex (and Microsoft Teams) accept.
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

#: Adaptive Card schema version Webex supports. Later versions are rejected.
CARD_VERSION = "1.3"

_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"

#: Reserved ``data`` keys on our own actions. Namespaced so a card input named
#: "action" cannot collide with the routing key and forge a decision.
KEY_KIND = "kirocrew_kind"
KEY_CHOICE = "kirocrew_choice"
KEY_NONCE = "kirocrew_nonce"
KEY_REQUEST = "kirocrew_request"

#: Card kinds this module builds, so a press can be routed without guessing.
KIND_APPROVAL = "approval"
KIND_OPTIONS = "options"

# Webex documents "up to five buttons (actions)" in its overview and twenty in
# its limitations list. Five is the safe design bound, and it is also the point
# past which a row of buttons stops being easier than typing.
MAX_CARD_ACTIONS = 5


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap body/actions as a message attachment entry."""
    return {
        "contentType": CARD_CONTENT_TYPE,
        "content": {
            "$schema": _SCHEMA,
            "type": "AdaptiveCard",
            "version": CARD_VERSION,
            "body": body,
            "actions": actions,
        },
    }


def _submit(title: str, kind: str, choice: str, nonce: str, request_id: str) -> dict[str, Any]:
    """An ``Action.Submit`` carrying our routing key in its ``data``.

    ``associatedInputs: "none"`` because these cards collect no inputs — asking
    Webex to gather them would only widen what comes back.
    """
    return {
        "type": "Action.Submit",
        "title": title,
        "associatedInputs": "none",
        "data": {
            KEY_KIND: kind,
            KEY_CHOICE: choice,
            KEY_NONCE: nonce,
            KEY_REQUEST: request_id,
        },
    }


def _text(value: str, *, wrap: bool = True, weight: str = "") -> dict[str, Any]:
    block: dict[str, Any] = {"type": "TextBlock", "text": value, "wrap": wrap}
    if weight:
        block["weight"] = weight
    return block


def approval_card(tool: str, *, nonce: str, request_id: str) -> dict[str, Any]:
    """An Approve / Deny card for a tool-permission request.

    *tool* must already be neutralized by the renderer's ``_safe_tool_label``.
    An Adaptive Cards 1.3 ``TextBlock`` renders a markdown SUBSET — including
    ``[text](url)`` — and there is no per-block switch to turn that off, so "put it
    in a TextBlock" is NOT the safety property here; the sanitized input is. A tool
    title is model-influenced text, and this card is the one message the user is
    being asked to trust.
    """
    return _card(
        [
            _text("Approve this tool?", weight="Bolder"),
            _text(tool or "this tool"),
        ],
        [
            _submit("✅ Approve", KIND_APPROVAL, "approve", nonce, request_id),
            _submit("🚫 Deny", KIND_APPROVAL, "deny", nonce, request_id),
        ],
    )


def usable_choices(choices: list[str]) -> list[str]:
    """The choices an options card can actually render, in button order.

    ONE derivation, shared by the card builder and by whoever records what the
    card offered, because a press comes back as an INDEX into this list: if the
    two sides derive it separately, dropping a single blank choice shifts every
    index after it and the button silently answers with its neighbour.

    Idempotent, so applying it at both layers is safe.
    """
    # Stripped before the emptiness test: a whitespace-only choice is truthy, so
    # a bare `if c` would render it as a blank button the user cannot read.
    return [stripped for c in choices if (stripped := c.strip())][:MAX_CARD_ACTIONS]


class LiveChoices:
    """The choices carried by the newest options card in each conversation.

    Lives OUTSIDE the renderer, and that is the whole point. An options card is
    sent at the very END of a turn, so the press necessarily arrives after the
    turn — and therefore after any per-turn object — has been torn down. A
    renderer-owned map answers every press with "no longer current", which is a
    card that is inert 100% of the time.

    Entries expire by REPLACEMENT (the conversation renders a newer card) or by
    being taken (a press is honoured), never by editing the card: Webex refuses to
    edit a message once it carries an attachment, so a spent card's buttons stay
    clickable forever and the entry is the only thing that can go stale.
    """

    __slots__ = ("_live",)

    #: Conversations to remember cards for. A generation bump (``/new``) mints a
    #: new session key, so an abandoned entry is unreachable but still resident;
    #: this bounds how many can accumulate over a long-lived gateway.
    MAX_TRACKED = 256

    def __init__(self) -> None:
        self._live: dict[str, tuple[str, tuple[str, ...]]] = {}

    def publish(self, session_key: str, nonce: str, choices: list[str]) -> None:
        """Record what the card just rendered for *session_key*."""
        if len(self._live) >= self.MAX_TRACKED and session_key not in self._live:
            self._live.pop(next(iter(self._live)), None)
        self._live[session_key] = (nonce, tuple(choices))

    def take(self, session_key: str, index: str, nonce: str) -> str:
        """The chosen text for a press, or ``""`` if it does not match.

        One-shot: an honoured press removes the entry, so the buttons Webex cannot
        retire answer exactly once. The press carries an INDEX, never text —
        treating a returned string as the choice would let a crafted press put
        arbitrary words into the turn — and the nonce is compared in constant time.
        """
        entry = self._live.get(session_key)
        if entry is None:
            return ""
        minted, choices = entry
        if not minted or not nonce or not secrets.compare_digest(nonce, minted):
            return ""
        if not index.isdigit():
            return ""
        position = int(index)
        if not 0 <= position < len(choices):
            return ""
        self._live.pop(session_key, None)
        return choices[position]


def options_card(choices: list[str], *, nonce: str) -> dict[str, Any] | None:
    """A one-button-per-choice card for an ``[OPTIONS:]`` trailer.

    *choices* must already be capped by
    :func:`kiro_crew.messaging.renderer.apply_options_cap`, which is what puts the
    overflow into the message body as a numbered list continuing the button slots
    — so widget and text form ONE list, the same contract every other widget
    channel follows. This truncates as a last resort rather than trusting that:
    a card Webex rejects for having too many actions would lose the choices
    entirely, where a truncated one still shows most of them alongside the
    numbered remainder.

    ``None`` when there is nothing to render.

    The choice text rides both the button title and its ``data``, and the press
    handler treats the returned value as an INDEX into the choices it rendered
    rather than as text to act on, so a crafted value cannot become an
    instruction.
    """
    usable = usable_choices(choices)
    if not usable:
        return None
    return _card(
        [_text("Pick one", weight="Bolder")],
        [
            _submit(choice[:60], KIND_OPTIONS, str(index), nonce, "")
            for index, choice in enumerate(usable)
        ],
    )


def read_press(inputs: Any) -> tuple[str, str, str, str]:
    """Destructure a card press into ``(kind, choice, nonce, request_id)``.

    Everything is coerced to ``str`` and unknown keys are ignored: ``inputs`` is
    a map Webex assembled from a client, so it is untrusted input rather than a
    structure to trust the shape of. A press that carries none of our reserved
    keys yields empty strings, which every caller reads as "not ours".
    """
    if not isinstance(inputs, dict):
        return "", "", "", ""
    return (
        str(inputs.get(KEY_KIND) or ""),
        str(inputs.get(KEY_CHOICE) or ""),
        str(inputs.get(KEY_NONCE) or ""),
        str(inputs.get(KEY_REQUEST) or ""),
    )
