"""Private owners composed by :mod:`kiro_crew.dashboard.chat_handlers`.

The dashboard chat HTTP handlers are split by responsibility across the modules of
this package, and ``kiro_crew.dashboard.chat_handlers`` stays their only import path
and their only patch surface: nothing but the handlers module imports an owner.

Each owner holds handlers and helpers moved verbatim from the one-module file.
:func:`kiro_crew._facade_compose.compose` makes every function an owner defines -- its
module functions and the methods of the classes it defines -- run on the handlers
module's globals rather than its own. Tests patch the handlers' names (``chat_handlers.sel``,
``.save_slot_off_loop``, ``._persist_handover_tail`` and dozens more). A function
that read its owner's globals would keep calling the unpatched object and the test
would pass while exercising nothing, so the handlers stay ONE namespace, as the
one-module file was. Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the handlers
  module only under ``TYPE_CHECKING``, and stdlib and third-party names (such as
  ``aiohttp.web``) plainly; both serve the type checker and the linter. Every name a function reads must exist in
  ``kiro_crew.dashboard.chat_handlers`` -- which is why that module keeps imports
  only its owners read, marked ``# noqa: F401`` -- and
  ``test_chat_handlers_composition_contract.py`` sweeps each function's bytecode to
  prove it does.
* A function's ``__module__`` reads ``kiro_crew.dashboard.chat_handlers`` as it did
  before the split, so reprs and pickling by reference are unchanged, and it logs
  through that module's ``logger``. Its source file differs, so a log record's
  ``module``, ``filename`` and ``lineno`` name the owner file. An owner's classes
  keep their own module, which is where ``inspect`` looks for a class's source.
* State stays on the handlers module. Every module-level value (the lock
  registries, the in-flight unlink task set, ``_test_interleave``, the constants) is
  a handlers global that owner functions reach by name, so a test that rebinds it
  there is the binding every function sees.

Which responsibility each owner holds, and which constructs stay in the handlers
module because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (the ``chat_handlers.py`` +
``chat_api/`` entry of the Dashboard module list).
"""
