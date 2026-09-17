"""Asana connector: pure vendor-protocol logic (W08).

This package holds the network-free, credential-free vendor logic the Asana
provider stream reuses: task/project field models and input/output
normalization, the offset/limit pagination contract, the HTTP+JSON error
classification, and the batch-partial-failure / ambiguous-create semantics.
The work-stream DAG that sequences it lives in
``docs/system-specs/modules/connector-capability-manifest.md`` (W08).

WHAT THIS OWNS vs WHAT IT DOES NOT
==================================
This package lives under ``kiro_crew.connections.vendors`` (the connector
campaign's vendor-logic home; the ``vendors`` common container is owned by
W01, not by this leaf). It is distinct from the shipped account-link surface
of ``kiro_crew.connections`` itself (OAuth clients, minting, L0/L1 probes) and
from ``kiro_crew.knowledge.connectors`` (knowledge-base ingestion). It owns
ONLY Asana's vendor-protocol shaping, and deliberately owns none of:

* Asana auth or a live call runtime. ``connections`` owns account binding;
  the operation-level adapter seam is W01's and is not landed yet. Everything
  here is standalone logic that does not import or depend on that seam, so it
  is testable and reviewable before W01 exists. Real wiring waits for W01.
* MCP tool NAMING and alias declarations. Asana's MCP tool names
  (``get_tasks``, ``get_projects``, ``create_tasks``, ``update_tasks``,
  ``delete_task``, ``add_comment`` ...) collide readily across the many-provider
  registry. Alias declarations are owned by ``registry.json``'s ``tool_aliases``
  field (resolved by :mod:`kiro_crew.connections.tool_aliases`), which is core
  registry outside this leaf's scope. This package therefore declares no Asana
  aliases; recording the collision aliases in that registry row is a follow-up
  for the registry owner. This is a NOTED GAP, not a commitment made here.

EVIDENCE AND UNKNOWNS
=====================
Every claim modeled here is grounded in the campaign's official-docs evidence
pass. Where the evidence records an UNKNOWN, this code preserves it as an
unknown rather than asserting a value it cannot cite (see each module's own
docstring for the specific unknowns it refuses to guess):

* MCP tool JSON schemas are not published; the authoritative source is the
  live ``tools/list`` command. Nothing here hard-codes an MCP parameter schema.
* The exact HTTP status for a cross-workspace GID denial (403 vs 404) is not
  observed; :mod:`kiro_crew.connections.vendors.asana.errors` classifies it without
  asserting either code.
* Offset-token TTL is undocumented; :mod:`kiro_crew.connections.vendors.asana.pagination`
  treats a token as expirable rather than permanent.
* ``templates`` has zero evidence coverage; this package invents no template
  operation or field -- that is left as an explicit follow-up gap.

TWO AUTHORIZATION SURFACES, NEVER MERGED
========================================
Asana's MCP app authorization and its native REST OAuth app are separate,
non-interchangeable surfaces (MCP tokens do not work against REST, and vice
versa). :mod:`kiro_crew.connections.vendors.asana.auth` models them as two distinct
concepts and refuses to collapse them into one.
"""
