"""The OPTIONS record and block helpers the Slack options lifecycle tests share.

``_recs`` reads the OPTIONS records for a slot or key through the accessor the code
uses and ``_set_recs`` seeds them; ``_is_live_control`` says whether blocks carry a
clickable control and ``_context_text`` joins their context text; ``_slack_app`` is an
aiohttp app serving the slot's Slack-link route. ``test_slack_options_lifecycle.py``
and its ``_expiry`` and ``_link`` siblings import what they use of these by name.
"""

from __future__ import annotations

from aiohttp import web

from kiro_crew.slack.format import OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION


def _recs(state, key):
    """The OPTIONS records for *key*, read through the accessor the code uses.

    Accepts a slot or a key. Given a slot it looks under the slot's EFFECTIVE
    session key and, failing that, under its bare name -- test fixtures record
    under whichever of the two reads more naturally, and the point of an assertion
    here is which records exist, not which spelling filed them. Production has no
    such ambiguity: record and expiry both resolve the live owner the same way, and
    the accessor canonicalises what it is given.
    """
    from kiro_crew.dashboard.chat_utils import effective_session_key, options_records

    if hasattr(key, "key"):
        slot = key
        return options_records(state, effective_session_key(slot)) or options_records(
            state, slot.key
        )
    return options_records(state, key)


def _set_recs(state, key, records):
    """Seed OPTIONS records for *key* (the store is keyed by session key)."""
    from kiro_crew.dashboard.chat_utils import effective_session_key, set_options_records

    if hasattr(key, "key"):
        key = effective_session_key(key)
    set_options_records(state, key, records)


def _is_live_control(blocks: list[dict]) -> bool:
    """True when *blocks* contain a clickable OPTIONS control."""
    return any(
        el.get("action_id") in (OPTIONS_CHECKBOXES_ACTION, OPTIONS_SUBMIT_ACTION)
        for b in blocks
        if b.get("type") == "actions"
        for el in b.get("elements", [])
    )


def _context_text(blocks: list[dict]) -> str:
    return " ".join(
        el.get("text", "")
        for b in blocks
        if b.get("type") == "context"
        for el in b.get("elements", [])
    )


def _slack_app(state):
    from kiro_crew.dashboard.chat_slack import api_chat_slot_slack_link

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    return app
