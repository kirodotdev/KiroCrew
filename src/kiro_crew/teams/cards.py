"""Adaptive Cards: the interactive surface Teams actually has.

Teams renders no Block Kit and no message components, but it does render
Adaptive Cards, and an ``Action.Submit`` on one comes back as an ORDINARY
``message`` activity whose ``value`` holds the button's data payload. That is
what makes tool approval and ``[OPTIONS:]`` chips reachable here.

``Action.Submit`` is deliberate, and ``Action.Execute`` is deliberately NOT used:
a universal action arrives as an ``invoke`` activity that must be answered with a
synchronous ``{statusCode, type, value}`` body, which is incompatible with the
fast-ack-then-background-turn shape the Connector's ~15s inbound timeout forces.
A submit needs no synchronous answer, so the existing ingress works unchanged.

Two safety properties live here:

* **A stale button fails closed.** ACP request ids restart at 1 for every
  provider/gateway process, so a card left in a Teams chat from a previous run
  can carry a request id that is live again for a DIFFERENT tool. Every prompt
  therefore mints a random nonce, and a submit whose nonce does not match the
  pending prompt is refused rather than treated as an answer to it.
* **The card carries no authority.** The payload is data a client sends, so it is
  only ever a LOOKUP key into state this process already holds. Nothing about the
  decision — which tool, which session, whether it is still pending — is read out
  of the submit.

Card version is pinned to 1.4: Teams supports up to 1.6, but 1.4 is the floor
every currently-supported Teams client renders, and nothing here needs a later
feature.
"""

from __future__ import annotations

from typing import Any

from kiro_crew.messaging.commands import YOLO_SCOPE_NOTE

#: Adaptive Card schema version. See the module docstring for why not 1.6.
CARD_VERSION = "1.4"
CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"

#: Discriminator on a submit payload, so an approval click, an options pick and
#: an unrelated card cannot be confused for one another.
KIND_APPROVAL = "kc_approval"
KIND_OPTION = "kc_option"
KIND_SESSION = "kc_session"

#: Approval decisions a card can carry.
DECISION_APPROVE = "approve"
DECISION_TRUST = "trust"
DECISION_DENY = "deny"
_DECISIONS = frozenset({DECISION_APPROVE, DECISION_TRUST, DECISION_DENY})


def _card(body: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap card content in the attachment envelope the Connector expects."""
    return {
        "contentType": CARD_CONTENT_TYPE,
        "content": {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": CARD_VERSION,
            "body": body,
            "actions": actions,
        },
    }


def _text_block(
    text: str, *, weight: str = "default", wrap: bool = True, subtle: bool = False
) -> dict[str, Any]:
    block: dict[str, Any] = {"type": "TextBlock", "text": text, "wrap": wrap}
    if weight != "default":
        block["weight"] = weight
    if subtle:
        # Secondary copy: present on every prompt, so it must not compete with the
        # question. 1.4 renders both properties on every supported Teams client.
        block["isSubtle"] = True
        block["size"] = "small"
    return block


def approval_card(
    *,
    title: str,
    purpose: str,
    request_id: str,
    nonce: str,
) -> dict[str, Any]:
    """The Approve / Approve + auto-approve / Deny card for one tool request.

    Three buttons, and the middle one is what lets a user stop being asked per
    tool: it arms the ONE process-wide auto-approve grant the dashboard toggle and
    ``/yolo`` drive. Teams keeps no grant of its own on purpose -- a channel-local
    trusted set would be a second grant with its own lifetime and its own way to
    disagree with the dashboard about whether auto-approve is on. The label says
    "Auto-approve" rather than "Trust session" for the same reason: the blast radius
    is every surface until it expires, and the button has to say so.

    ``style`` is omitted on every action on purpose -- Teams does not render
    Adaptive Card positive/destructive action styling, so setting it would be a
    declaration with no effect.
    """
    body = [_text_block(f"Run `{title}`?", weight="bolder")]
    if purpose:
        body.append(_text_block(purpose))
    # The middle button's scope, stated BEFORE the press. "Auto-approve" on a button
    # inside one chat reads as scoped to that chat, and the grant is process-wide --
    # so without this line a user over-grants on every prompt and only finds out from
    # the reply afterwards. The shared note, so it cannot disagree with `/yolo`.
    body.append(_text_block(f"Auto-approve: {YOLO_SCOPE_NOTE}", subtle=True))
    data = {"kc": KIND_APPROVAL, "rid": request_id, "nonce": nonce}
    actions = [
        {
            "type": "Action.Submit",
            "title": "Approve",
            "data": {**data, "decision": DECISION_APPROVE},
        },
        {
            "type": "Action.Submit",
            "title": "Approve + auto-approve",
            "data": {**data, "decision": DECISION_TRUST},
        },
        {"type": "Action.Submit", "title": "Deny", "data": {**data, "decision": DECISION_DENY}},
    ]
    return _card(body, actions)


def options_card(*, prompt: str, options: list[str], nonce: str) -> dict[str, Any]:
    """Render a parsed ``[OPTIONS:]`` trailer as tappable chips.

    The caller has already applied ``capabilities.max_buttons`` through the
    shared ``apply_options_cap``, so whatever arrives here is what fits; any
    overflow is already in ``prompt`` as a numbered list.
    """
    body = [_text_block(prompt)] if prompt else []
    actions = [
        {
            "type": "Action.Submit",
            "title": label,
            "data": {"kc": KIND_OPTION, "nonce": nonce, "index": index, "label": label},
        }
        for index, label in enumerate(options)
    ]
    return _card(body, actions)


def session_picker_card(*, prompt: str, choices: Any, nonce: str) -> dict[str, Any]:
    """The ``/sessions`` picker: one Submit per offered dashboard session.

    The payload carries only the nonce and the INDEX -- never the session key. A submit
    is client input, so a key in it would be an instruction to bind whatever the sender
    named; the index is resolved against the list this process actually offered, so a
    forged or replayed press can only ever miss.
    """
    body = [_text_block(prompt)] if prompt else []
    actions = [
        {
            "type": "Action.Submit",
            "title": f"{index + 1}. {choice.title}",
            "data": {"kc": KIND_SESSION, "nonce": nonce, "index": index},
        }
        for index, choice in enumerate(choices)
    ]
    return _card(body, actions)


def resolved_card(*, title: str, outcome: str) -> dict[str, Any]:
    """The card a prompt is REPLACED with once it has been answered.

    Editing the card away is what stops a Teams chat accumulating live buttons
    that resolve to nothing: an answered or expired prompt must not still look
    actionable. Carries no actions at all, so there is nothing left to click.
    """
    return _card([_text_block(f"`{title}` — {outcome}")], [])


#: Digits allowed in a submit's ``index``. See ``parse_submit``.
_MAX_INDEX_DIGITS = 3


def parse_submit(value: Any) -> dict[str, str] | None:
    """Read a card submit payload, or None when it is not one of ours.

    Deliberately strict and total: a submit is client-supplied, so every field is
    validated for presence, type AND range here rather than at a use site. Returns a
    flat ``str``-valued mapping so a caller cannot accidentally propagate a
    nested attacker-shaped object.
    """
    if not isinstance(value, dict):
        return None
    kind = value.get("kc")
    nonce = value.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return None
    if kind == KIND_APPROVAL:
        rid = value.get("rid")
        decision = value.get("decision")
        if not isinstance(rid, str) or not rid:
            return None
        if not isinstance(decision, str) or decision not in _DECISIONS:
            return None
        return {"kc": KIND_APPROVAL, "rid": rid, "nonce": nonce, "decision": decision}
    if kind == KIND_SESSION:
        index = value.get("index")
        if isinstance(index, bool) or not isinstance(index, (int, str)):
            return None
        text = str(index)
        if not text.isdigit() or len(text) > _MAX_INDEX_DIGITS:
            return None
        return {"kc": KIND_SESSION, "nonce": nonce, "index": text}
    if kind == KIND_OPTION:
        label = value.get("label")
        if not isinstance(label, str) or not label:
            return None
        index = value.get("index")
        # Teams may return a number as a string depending on the client, so accept
        # either shape but normalize to a digit string.
        if isinstance(index, bool) or not isinstance(index, (int, str)):
            return None
        text = str(index)
        # Bounded, not just numeric: a caller does a bare ``int()`` on this, and
        # Python raises past ``sys.get_int_max_str_digits()`` (4300) -- which would
        # escape as a traceback and leave a dead button, the exact outcome this
        # module's strictness exists to avoid. No legitimate index can exceed
        # ``max_buttons``, so three digits is generous.
        if not text.isdigit() or len(text) > _MAX_INDEX_DIGITS:
            return None
        return {"kc": KIND_OPTION, "nonce": nonce, "index": text, "label": label}
    return None
