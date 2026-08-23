"""Kiro Crew Auto Triage Pipeline — a full-page pipeline view of Kiro Crew's
own auto-triage crews.

One horizontal lane per crew work item across the phase enum: which phase each
item is in right now, where items are piling up, and which have stalled. This is
the first tenant of the "a repo plugs in its own pipeline" idea — Issue Radar is
for ANY repo, while this pipeline is OURS, one specific automation.

Manifest-only, like ``projects``, ``agent_worlds`` and ``channels``: this app
ships NO backend routes and re-exports NO ``register_routes``. It reads its data
through Issue Radar's existing, already-green crew-fabric seam
(``GET /api/apps/issue-radar/crew/fabric``) — the repo-agnostic data half
(``phase`` on the ledger event line, ``fold_fabric``, the route) stays in
``issue_radar`` on purpose. The package exists only so
``discover_builtin_apps()`` finds the ``app.json`` next to it, the same way it
does for every other manifest-only builtin.

Because there is no ``register_routes`` here, the app is deliberately NOT listed
in ``kiro_crew.apps.builtins.BUILTIN_NAMES`` — that list is only for builtins
whose Python package registers routes or services at gateway startup.
"""
