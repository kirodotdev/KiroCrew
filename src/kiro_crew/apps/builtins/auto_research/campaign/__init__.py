"""Research Lab campaign engine, composed behind the ``handlers`` route facade.

One owner per concern; the import graph runs one way, top to bottom:

- ``untrusted`` -- the boundary for LLM- and user-authored text: the canonical
  credential/exfil-URL scrub (fail-closed) and the nonce fence for prompts.
- ``storage`` -- data-home paths, the campaigns SQLite schema and its on-loop
  guard, the campaign directory file interface, cycle-file discovery and the
  scrubbed row/finding reads.
- ``lifecycle`` -- validation, create/transition/delete, the per-campaign
  transition lock, generation-fenced background transitions, SSE fan-out and
  the SEL audit trail.
- ``publication`` -- documents a campaign publishes: the agent brief, the
  report, the artifact export and the Knowledge Library export.
- ``exploration`` -- emergent sub-questions, the reserve zone and FINALIZE MODE.
- ``agent_mode`` / ``workflow_mode`` -- the two execution adapters: the
  autonudge-driven worker (launch, stop, the ``worker_done.json`` marker and
  cycle accounting) and the Dynamic Workflow run (launch, cancel, poll, run id
  and cycle offset).
- ``watchdog`` -- the polling loop body: trust expiry, question pauses, the
  stall verdict and the terminal settlement of a run.
- ``grill`` -- the question-tree planner behind the grill endpoint.

``handlers`` keeps the HTTP adapters, route registration and the watchdog task
handle, and resolves every historic ``handlers.<name>`` to the owner above.

Two conventions keep that facade honest. A component reaches another
component's functions and mutable state through the module
(``storage._get_db()``), never a from-import, and a collaborator shared by
several components is imported by one of them only (``agent_mode.research_slot_key``),
so a patch applied through ``handlers`` or the owner is what every caller sees
at call time; only the ``CampaignStatus`` value type is imported by name. And every component logs
through ``LOGGER_NAME``, the historic logger, so operator log filters and levels
keyed on it match every component.
"""

LOGGER_NAME = "kiro_crew.apps.builtins.auto_research.handlers"
