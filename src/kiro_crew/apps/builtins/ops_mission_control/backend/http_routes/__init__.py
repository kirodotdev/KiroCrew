"""Ops Mission Control's HTTP projections, composed by ``backend.routes``.

``backend/routes.py`` stays the one module the gateway, the manifest, the security posture
registry and the tests address: it registers every route, gates each behind
``_require_enabled``, owns the SEL audit writer, the redaction floor and the ledger indexer,
and re-exports every other name it defined. The handler bodies live here, one module per
slice of the surface:

- ``_shared`` — request parsing, strict booleans, the coded store refusal, the gateway-state
  accessor for the Slack client, the surface's logger and the seam types.
- ``board`` — the read-only projections the dashboard polls to paint the board: ``/state``,
  ``/handover``, ``/incidents``, ``/incident``, ``/signals``, ``/providers``, ``/rotation``.
- ``lifecycle`` — incident lifecycle writes: ``/incident/transition``, ``/incident/claim``
  and ``/dispatch``.
- ``actions`` — provider-write authority: ``/incident/action``, the propose loop
  (``/incident/propose``, ``/incident/proposal/decide`` and its ``/proposals`` queue) and the
  permit that makes the autonomy gate a chokepoint.
- ``configuration`` — writes to this instance's configuration: provider config and secrets,
  ``/settings``, and which of the app's crons ``/rotation/arm`` leaves armed.
- ``ledger`` — the shared ledger: read, contradictions, write, hygiene and delete.
- ``webhook`` — the bounded, signed ``/webhook`` ingress.

**Dependency rule.** Nothing here imports ``routes``, and nothing here binds a facade seam at
module scope. A projection that calls one of the seams (``get_registry``, ``_audit``,
``_safe_outbound``, ``put_secret``, ``delete_secret``, ``merge_provider_config``,
``_index_ledger_safely``) takes it as a keyword-only parameter of the same name, with no
default, and the facade's same-named handler passes its own current binding on every call.
That is what keeps ``mock.patch.object(routes, <seam>)`` reaching every call site that reads
the seam, and why those parameters keep the facade's spelling, leading underscore included:
each body reads the facade's own names. A helper that calls a seam receives it from its
caller the same way, also keyword-only.
"""
