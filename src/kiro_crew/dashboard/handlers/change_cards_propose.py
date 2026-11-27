"""Propose one change card in a slot: the body every card route and a typed command share.

:func:`kiro_crew.dashboard.handlers.change_cards.propose_for_caller` answers an agent's
HTTP request with it, and a command the person typed in chat (``/mcp add``) calls it
with no request at all. Same validation, same preview, same store, same row in the
conversation, so a card raised either way is the same card.

Every helper is read from ``change_cards`` at call time, so a test's patch of
``change_cards.<name>`` reaches this path too.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


async def propose_in_slot(
    state: Any,
    slot_key: str,
    sk: str,
    *,
    kind: str,
    params: Any,
    reason: Any,
    app: Any = None,
    claim: Callable[[], Awaitable[Callable[[], Awaitable[None]]]] | None = None,
) -> dict[str, Any]:
    """Propose one card of *kind* in *slot_key* and return its public record.

    Raises ``CardError`` or ``CardCatalogError`` with the refusal. *app* is passed
    to the readers of the kinds that need one; *claim* is
    :func:`change_cards.propose_for_caller`'s, with the same contract.
    """
    from kiro_crew.dashboard.handlers import change_cards as routes

    cards, catalog, chat_cards = routes.cards, routes.catalog, routes.chat_cards
    # An app's kind and an app's card key are never a chat caller's: see
    # propose_app_card.
    if kind in catalog.APP_ONLY_KINDS or slot_key.startswith(
        (routes.APP_SLOT_PREFIX, routes.OWNER_SLOT_PREFIX)
    ):
        raise routes.CardError(403, "app_only", "only the owner can propose this change")
    try:
        params = catalog.validate_params(kind, params)
        params = await cards.resolve_params(kind, params)
        cards.check_param_text(params)
        reason = cards.clean_reason(reason)
        preview, before, context = await cards.build(kind, params, state=state, app=app)
        await routes._settle_state(state)
    except catalog.CardCatalogError as exc:
        if exc.code == "no_change":
            await routes._expire_made(state, slot_key, kind, params)
        raise
    store = routes.card_store_for(state)
    undo_claim: Callable[[], Awaitable[None]] | None = None
    try:
        if claim is not None:
            undo_claim = await claim()
        rec = store.propose(
            slot_key=slot_key,
            session_key=sk,
            kind=kind,
            params=params,
            reason=reason,
            preview=preview,
            before=before,
            context=context,
        )
    except (routes.CardError, catalog.CardCatalogError) as exc:
        if undo_claim is not None:
            await undo_claim()
        if isinstance(exc, routes.CardError) and exc.code == "duplicate_card":
            raise await routes._show_waiting_card_again(state, slot_key, kind, params, exc)
        raise
    try:
        await store.flush(strict=True)
    except routes.CardError as exc:
        # Never shown, never in the transcript: the proposal did not happen.
        store.discard(rec["id"])
        if undo_claim is not None:
            await undo_claim()
        raise routes.CardError(exc.status, exc.code, "the change card could not be saved")
    public = store.public(rec)
    # The card is part of the conversation from here on: its row lands at this
    # point of the transcript, before the frame that makes it live.
    await chat_cards.load_conversation_window(state, public)
    chat_cards.record_change_proposed(state, public)
    delivered = await routes._broadcast(state, public)
    routes._audit(sk, "cards.propose", "ok", resources=routes._card_resources(public))
    return {**public, "delivered_clients": delivered}
