"""Private owners composed by :mod:`kiro_crew.dashboard.handlers.files`.

The dashboard file HTTP handlers are split by responsibility across the modules of
this package, and ``kiro_crew.dashboard.handlers.files`` stays their only import
path and their only patch surface: nothing but the handlers module imports an owner.

Each owner holds handlers and helpers moved verbatim from the one-module file.
:func:`kiro_crew._facade_compose.compose` makes every function an owner defines -- its
module functions and the methods of the classes it defines -- run on the handlers
module's globals rather than its own. Tests patch the handlers' names (``files.is_sensitive_path``,
``._sel``, ``._run_path_probe``, ``._MAX_UPLOAD_BYTES`` and dozens more). A function
that read its owner's globals would keep calling the unpatched object and the test
would pass while exercising nothing, so the handlers stay ONE namespace, as the
one-module file was. Three consequences follow:

* An owner's imports are inert for its functions. An owner imports the handlers
  module only under ``TYPE_CHECKING``, and stdlib and third-party names (such as
  ``aiohttp.web``) plainly; both serve the type checker and the linter. Every name a
  function reads must exist in ``kiro_crew.dashboard.handlers.files`` -- which is
  why that module keeps imports only its owners read, marked ``# noqa: F401`` --
  and ``test_dashboard_files_composition_contract.py`` sweeps each function's
  bytecode to prove it does.
* A function's ``__module__`` reads ``kiro_crew.dashboard.handlers.files`` as it did
  before the split, so reprs and pickling by reference are unchanged, and it logs
  through that module's ``logger``. Its source file differs, so a log record's
  ``module``, ``filename`` and ``lineno`` name the owner file. An owner's classes
  keep their own module, which is where ``inspect`` looks for a class's source,
  except ``path_probe._PathProbeBusy``: a traceback renders an error by its module,
  so it reports ``kiro_crew.dashboard.handlers.files``, the path callers catch it by.
* State stays on the handlers module. Every module-level value (the upload and
  screenshot directories, the size caps, the extension tables, the probe pool
  budgets, ``logger``) is a handlers global that owner functions reach by name, so
  a test that rebinds it there is the binding every function sees. The one
  exception is ``git_panel._GIT_PANEL_STDOUT_CAP``: ``_run_git_bounded`` reads it
  as a parameter default, which Python evaluates when the owner loads, so the
  constant is defined beside that function and the handlers module imports it.

Which responsibility each owner holds, and the files that hold each construct a
repository guard reads by path, are recorded in
``docs/system-specs/modules/learn-cron-dashboard.md`` (the "Dashboard file API
owners" paragraph under Key Endpoints).
"""
