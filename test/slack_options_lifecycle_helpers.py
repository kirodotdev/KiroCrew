"""The OPTIONS record and block helpers the Slack options lifecycle tests share."""

from __future__ import annotations

from aiohttp import web

from kiro_crew.slack.format import is_options_actions_block


def _recs(state, key):
    """The OPTIONS records for *key*, read through the accessor the code uses.

    Given a slot it looks under the EFFECTIVE session key, then the bare name, since
    fixtures record under either; production resolves the live owner one way.
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
    return any(is_options_actions_block(b) for b in blocks)


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
