"""Crewmate SDK — an app proposes one crewmate; only the owner's press creates it.

Declared as ``permissions.proposeCrewmate`` and injected as ``ctx.crewmate``,
``None`` otherwise (rfc-crewmate-share.md §5.5). ``propose`` hands core one
parsed ``.crewmate`` bundle. Core re-checks it (``crewmate_bundle.
crewmate_import_params``, fail closed) and raises one ``crewmate.import``
change card keyed ``app:<name>``. The SDK creates nothing and reads no crewmate:
the card's plan runs only on the owner's press, through owner routes that refuse
an app token.
"""

from __future__ import annotations

from typing import Any

#: Off until the dashboard lists and cancels ``app:`` cards: until
#: then an app's card could only expire unpressed, so ``ctx.crewmate`` stays
#: ``None`` even for an app that declares the permission.
PREVIEW_ENABLED = False


class CrewmateProposalError(ValueError):
    """Core refused the bundle or the card; ``code`` says why, as the card routes do."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CrewmateSDK:
    """``ctx.crewmate`` for one app. *http_app* is the gateway's Application."""

    def __init__(self, app_name: str, http_app: Any) -> None:
        self._app_name = app_name
        self._http_app = http_app

    async def propose(self, bundle: dict[str, Any]) -> str:
        """Raise the import card for *bundle*; returns its card id. Never a crew key."""
        from kiro_crew import change_card_catalog as catalog
        from kiro_crew.crewmate_bundle import crewmate_import_params
        from kiro_crew.dashboard.change_cards import CardError
        from kiro_crew.dashboard.handlers.change_cards import propose_app_card

        try:
            params = crewmate_import_params(bundle)
            card = await propose_app_card(self._http_app, self._app_name, params)
        except (catalog.CardCatalogError, CardError) as exc:
            raise CrewmateProposalError(exc.code, exc.message) from None
        return str(card["id"])

    async def status(self, card_id: str) -> dict[str, Any]:
        """``{"status": ...}`` of this app's own card, plus ``"crew"`` once applied."""
        from kiro_crew.dashboard.change_cards import CardError
        from kiro_crew.dashboard.handlers.change_cards import app_card_status

        if not isinstance(card_id, str) or not card_id:
            raise CrewmateProposalError("invalid_card_id", "card_id is required")
        try:
            return await app_card_status(self._http_app, self._app_name, card_id)
        except CardError as exc:
            raise CrewmateProposalError(exc.code, exc.message) from None
