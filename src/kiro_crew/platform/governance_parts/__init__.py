"""The owner modules :mod:`kiro_crew.platform.governance` is composed from.

``kiro_crew.platform.governance`` stays the governance model's import path and its patch
surface: callers import every name from it, and tests patch the names there. Each module
here owns one responsibility; the facade imports them lowest layer first, and a module
imports only the ones listed before it, never the facade:

* ``matching`` -- how an item matches a pattern: the enforcer-owned strictness scales,
  the five matchers and their registry, item normalization and host extraction;
* ``tool_calls`` -- what a tool call touches: the computer-use action classes, the
  tool-title classification and the (scope, item) pairs a call's arguments name;
* ``archetypes`` -- the decision record, the four control archetypes and the algebra
  that composes two controls of one archetype;
* ``catalog`` -- which scopes exist: ``SCOPE_CATALOG`` with its archetype ids,
  ``register_scope`` and the scope aliases;
* ``carriers`` -- what a parsed policy and profile hold: ``GovernanceCeiling`` and
  ``Profile`` with their boot, update, distribution and agentcore blocks, and profile
  inheritance;
* ``parsing`` -- turning a policy or profile document into those carriers;
* ``signature`` -- proving a policy's ``identity.signature``, and the opt-in gate that
  refuses an unproven one;
* ``tiers`` -- the tier ladder: where each local policy is read, the per-process tier
  state, and the fold into the installed ceiling;
* ``evaluator`` -- answering a query: ``resolve``, ``resolve_ordinal``, ``gate_decision``
  and the command force-pins.

The facade keeps the auto-approve surface it decides itself -- ``may_skip_gate`` with
the builtin-tool scope maps, the auto-approve strip, the confirm-tools gate,
``may_skip_gate_now`` and ``sanitize_agent_config_governance`` -- and the two boot
checks, ``assert_governance_floor`` and ``assert_governance_paths_protected``. Its
mirror (:mod:`kiro_crew._facade_mirror`) carries a patch of
``kiro_crew.platform.governance.<name>`` to every module that binds the name, so the
moved code reads its own globals unchanged.
"""
