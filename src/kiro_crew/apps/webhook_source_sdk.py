"""``ctx.webhook_source``: the ``webhooks`` governance answer for an app's own ingress.

An app that takes inbound deliveries on its own routes (a signed alert webhook, say)
declares ``"permissions": {"webhookSource": true}`` in ``app.json``. Its hook context
then carries this SDK, and the app asks :meth:`WebhookSourceSDK.refusal` before it
checks its own switch, secret or signature, exactly as a built-in asks
``webhook_sources.source_refusal`` today.

The source id is the app's name, taken from the context, so an app cannot ask the
policy about another source. Every call re-reads the live declaration
(``webhook_sources.app_source_refusal``): a manifest that drops ``webhookSource``
refuses the next delivery. A context built without the declaration carries
``None``, and an app must then refuse its webhook rather than accept it ungoverned.

Grants no reach. The route, its authentication by the gateway token, the app's own
signature check, body cap and audit rows stay the app's; this SDK only lets the
policy answer for it. Holds the app's name and nothing else, so it is stateless.
"""

from __future__ import annotations

import asyncio


class WebhookSourceSDK:
    """The ``webhooks`` scope's answer for one app's inbound route."""

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name

    @property
    def source(self) -> str:
        """The source id a policy names to permit this app's webhook: its own name."""
        return self._app_name

    def refusal(self) -> str:
        """Why the policy refuses this app's inbound webhook; ``""`` when it permits.

        Blocking (the governance answer can read the profile store), so a route
        handler calls :meth:`refusal_async`, or this from a worker thread.
        """
        from kiro_crew.webhook_sources import app_source_refusal

        return app_source_refusal(self._app_name)

    async def refusal_async(self) -> str:
        """:meth:`refusal` off the event loop."""
        return await asyncio.to_thread(self.refusal)
