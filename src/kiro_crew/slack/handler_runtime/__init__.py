"""Private owners composed by :mod:`kiro_crew.slack.handler`.

The native Slack turn path's rules are split by responsibility across the modules of this
package, and ``kiro_crew.slack.handler`` stays their only import path and their only patch
surface: nothing but the handler module imports an owner.

Each owner holds functions and classes moved verbatim from the one-module file.
:func:`kiro_crew._facade_compose.compose` makes every function an owner defines -- its
module functions and the methods of the classes it defines -- run on the handler
module's globals rather than its own. The gateway, the dashboard, the messaging transport and the tests reach these names
through the handler module and patch them there (``handler.is_yolo_mode``,
``._PendingApproval``, ``._build_approval_blocks``, ``.sel``, ``.kiro_agents_dir``,
``._EDIT_INTERVAL`` and dozens more). A function that read its owner's globals would keep
calling the unpatched object and the test would pass while exercising nothing, so the
handler stays ONE namespace, as the one-module file was. Four consequences follow:

* An owner's imports are inert for its functions. An owner imports the handler module
  only under ``TYPE_CHECKING``, and stdlib names plainly; both serve the type checker and
  the linter. Every name a function reads must exist in ``kiro_crew.slack.handler``, and
  ``test/test_slack_handler_composition_contract.py`` sweeps each function's bytecode to
  prove it does.
* A function's ``__module__`` reads ``kiro_crew.slack.handler`` as it did before the
  split, so reprs and pickling by reference are unchanged and it logs through that
  module's ``logger``. Its source file differs, so a log record's ``module``, ``filename``
  and ``lineno`` name the owner file. An owner's classes keep their own module, which is
  where ``inspect`` looks for a class's source.
* State stays on the handler module. Every module-level value -- the constants, the
  pending and linked approval registries, the voice state, the owner id and allowlist,
  the per-thread agent and project maps, the phase-emoji table, the privacy and
  auto-title tracker aliases -- is a ``kiro_crew.slack.handler`` global that owner
  functions reach by name, so a test that rebinds one there is the binding every
  function sees, and a ``global`` statement inside a rebound owner function writes the
  handler's namespace.
* The handler's own body runs BEFORE its ``compose`` call, so nothing in it may call an owner
  function: the phase table built at import keeps ``_build_phase_emojis`` in the handler
  module for that reason.

Which responsibility each owner holds, and which constructs stay in the handler module
because repository guards read them there, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (the "Native handler composition" section).
"""
