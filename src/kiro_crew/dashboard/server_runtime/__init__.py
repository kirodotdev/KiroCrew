"""Private owners composed by :mod:`kiro_crew.dashboard.server`.

The dashboard gateway's bootstrap is split by responsibility across the modules of
this package, and ``kiro_crew.dashboard.server`` stays their only import path and
their only patch surface: nothing but the server module imports an owner.

Each owner holds functions moved verbatim from the one-module file, plus the boot
phases ``start_dashboard`` and ``start_api_server`` delegate to.
:func:`kiro_crew._facade_compose.compose` makes every function an owner defines -- its
module functions and the methods of the classes it defines -- run on the server
module's globals rather than its own.
Tests patch the server's names (``server.sel``, ``._start_site``, ``._bind_once``,
``.data_home``, ``.current_context``, ``._extra_frame_ancestors`` and dozens more),
and a function that read its owner's globals would keep calling the unpatched
object while the test passed exercising nothing, so the server stays ONE
namespace, as the one-module file was. Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the server
  module only under ``TYPE_CHECKING``, and stdlib and third-party names (such as
  ``aiohttp.web``) plainly; both serve the type checker and the linter. Every name a
  function reads must exist in ``kiro_crew.dashboard.server`` -- which is why that
  module keeps imports only its owners read, marked ``# noqa: F401`` -- and
  ``test_dashboard_server_composition_contract.py`` sweeps each function's bytecode
  to prove it does.
* A function's ``__module__`` reads ``kiro_crew.dashboard.server`` as it did before
  the split, so reprs, pickling by reference and every route walk that selects
  handlers by ``__module__`` are unchanged, and it logs through that module's
  ``logger``. Its source file differs, so a log record's ``module``, ``filename``
  and ``lineno`` name the owner file. An owner's classes keep their own module,
  which is where ``inspect`` looks for a class's source.
* State stays on the server module. Every module-level value (the internal-path
  sets, the CSP and cache policies, the timeouts and budgets, the tailnet awake
  cache, the own-address warm task set, ``logger``) is a server global that owner
  functions reach by name, so a test that rebinds it there is the binding every
  function sees. No owner defines a module-level value.

Which responsibility each owner holds, and which constructs stay in the server
module because repository guards read them there, is recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (the ``server.py`` bullet
under "Dashboard").
"""
