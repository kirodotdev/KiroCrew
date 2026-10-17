"""Private owners composed by :mod:`kiro_crew.slack.gateway`.

The gateway is split by responsibility across the modules of this package, and
``kiro_crew.slack.gateway`` stays its only import path and its only patch surface:
nothing but the facade imports an owner.

Each owner holds the gateway's functions and classes, moved verbatim or, for the cron
callback and the sub-agent completion consumer, cut out of their closures with only
re-plumbing edits (closure inputs become parameters, calls to former closures become
``self.`` calls); the list is in
``docs/system-specs/modules/slack-gateway.md`` (Composition).
A function whose first parameter is ``self`` is an orchestrator method: the facade
binds it as the ``GatewayOrchestrator`` attribute of the same name, so every call
site, ``patch.object`` target, ``GatewayOrchestrator.__new__`` fixture and unbound
``GatewayOrchestrator.<name>(stub, ...)`` call reaches it exactly as before. The
orchestrator remains the one holder of the state those methods act on; an owner
keeps no state of its own.

:func:`kiro_crew._facade_compose.compose`, called with ``GatewayOrchestrator`` as its
``host``, makes every function an owner defines -- its module functions, those
methods, and the methods of the classes it defines -- run on the facade's module
globals rather than its own. Tests patch the gateway's names
(``kiro_crew.slack.gateway.sel``, ``.stream_and_collect``, ``.CronService`` and a
hundred more). A function that read its owner's globals would keep calling the
unpatched object and the test would pass while exercising nothing, so the gateway
stays ONE namespace, as the one-module file was. Two consequences follow:

* An owner's imports are inert for its functions. Apart from what a class
  definition needs at import time, they sit under ``TYPE_CHECKING``, for the type
  checker and the linter: a standard-library module by its own name, so a gate
  that resolves a call from the file's own imports (``check_sync_io_in_async``)
  still reads it, and every other name from the facade
  (``test_gateway_runtime_bindings.py`` lists the owners that still take a gated
  module from the facade). Every name a function reads must exist in
  ``kiro_crew.slack.gateway``, and ``test_slack_gateway_composition_contract.py``
  sweeps each function's bytecode to prove it does.
* A function's ``__module__`` and ``__qualname__`` read as they did before the
  split -- ``kiro_crew.slack.gateway`` and ``GatewayOrchestrator.<name>`` -- so
  reprs, pickling by reference and the names of nested functions are unchanged;
  only its source file differs. An owner's classes keep their own module, which is
  where ``inspect`` looks for a class's source.

Which responsibility each owner holds, and which constructs stay in the facade
because repository guards read them there, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""
