"""Crew Companion — a desktop companion that helps you pace your day.

It nudges you to take breaks (water, stretch, look away) on an interval you
choose, holds the reminders you set in plain language, and runs a short breathing
exercise when you need to reset. It also tells you when a session finishes,
fails, or needs your OK.

Architecture — three parts, no separate application:

* **This Python package** owns the state and the clock: the reminder store, the
  break rotation, the daily breathing-prompt budget, and the tick that decides
  what fires. It runs IN-PROCESS in the gateway, like ``auto_research`` and
  ``issue_radar``.
* ``website/src/apps/crew-companion/`` owns the dashboard page and the panel,
  served by the gateway and therefore same-origin with the dashboard — plain
  ``fetch(..., {credentials: 'same-origin'})`` authenticates. It also keeps the
  natural-language reminder parser, which stays in TypeScript deliberately: it is
  hardened, and re-implementing 900 lines of span-alignment and day-part rules in
  a second language would re-earn every bug they took ten review rounds to remove.
* ``website/electron/crew-companion/`` owns the window layer — the transparent,
  always-on-top overlay the companion is drawn in. Those need Electron
  main-process APIs, so they live in Kiro Crew's existing shell rather than
  shipping a second Electron runtime.

WHAT THIS REPLACED, AND WHY IT MATTERS
--------------------------------------
The companion used to be a SEPARATE macOS application. This builtin was a
connector to it: the manifest declared ``mcpServers.crew-companion.url =
http://127.0.0.1:7778/mcp`` and ran ``open "$HOME/Applications/Crew
Companion.app"`` as an ``onEnable`` script. Because the enable path rolls back
when that script fails, and because the app was never shipped, downloadable or in
any registry, **the tile could not be enabled by anyone but its author** — on
whose machine the app happened to exist from a local build.

Nothing here launches anything, so there is nothing to fail and nothing to roll
back. Enabling the app and the app working are now the same state.
"""

# Required re-export: dashboard/server.py's startup route registration imports
# the PACKAGE and checks hasattr(_mod, "register_routes") — the same convention
# mochi/__init__.py and issue_radar/__init__.py follow (real call site:
# server.py:2434, inside the `for _builtin_name in BUILTIN_NAMES` loop). Routes
# are registered at STARTUP, not on enable, which is why every handler carries its
# own enabled check and answers 403 while the app is off.
from kiro_crew.apps.builtins.crew_companion.backend.routes import (  # noqa: F401,E402
    register_routes,
)
