"""Discord channel — Gateway WebSocket transport onto the shared TurnDriver.

Modules mirror the Telegram channel's layout:

* ``client``            — low-level Gateway WS + REST client (pure aiohttp)
* ``transport``         — :class:`MessagingTransport` implementation
* ``renderer``          — :class:`Renderer` (streaming edits + button rows)
* ``commands``          — ``!new`` / ``!compact`` / … text-command parsing
* ``transport_dispatch``— dispatcher driving turns on the shared ``TurnDriver``
* ``gateway``           — guarded startup entry point for the orchestrator
"""
