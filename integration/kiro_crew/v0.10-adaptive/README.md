# KiroCrew 0.10 adaptive adapter

Source-audited against KiroCrew `0.10.0-insider.4` (`e9bfada`) on 2026-10-17.

KiroCrew 0.10 split reasoning-effort and selection-capability handlers into
`dashboard/chat_api/slot_effort.py` and composes those owner functions onto the
`dashboard/chat_handlers.py` namespace. This adapter keeps the 0.9 ACP/runtime
registry integration, patches the moved owner implementation, and imports the BYO
helper functions into the composed handlers namespace. It also makes dashboard
auto-title generation honor KiroCrew's background-role model when Kiro BYO is the
active harness, matching the status-card/summary background path. All seams remain
semantic and fail closed when an upstream source contract moves.

The 0.10 integration also preserves KiroCrew's shared model-catalogue contract:
BYO returns one stable Type+concrete list, never changes `/api/models` by route,
and never reloads the dashboard on SPA navigation. Existing slots are reconciled
at the routing-state boundary so a stale `Auto (Jev)` selection is converted to
the active BYO default Type in place, while explicit concrete model pins remain
untouched.
