"""Parts composed by :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`.

The Code Review Sage backend routes are split by responsibility across the modules of
this package, and ``backend/routes.py`` stays their only import path and their only
patch surface: nothing but that module imports a part.

Each part holds functions moved verbatim from the one-module file.
:func:`kiro_crew._facade_compose.compose` makes every function a part defines run on
the facade's globals rather than its own. The app's tests load a private copy of
``backend/routes.py`` per test and rebind its names (``_RUNS``, ``_save_runs``,
``_KNOWN_MODELS``, ``is_app_enabled`` and more), and each copy composes the parts onto
its own namespace, so the routes stay ONE namespace per load, as the one-module file
was. Three consequences follow:

* A part's imports are inert for its functions. A part imports the facade only under
  ``TYPE_CHECKING`` and stdlib names plainly. Every name a function reads must exist
  on the facade, which binds every name a part defines;
  ``test/test_routes_parts_sage_composition_contract.py`` sweeps each function's
  bytecode to prove it.
* A function's ``__module__`` reads the facade's name, as it did before the split, and
  it logs through the facade's ``logger``; its source file differs, so a log record's
  ``filename`` and ``lineno`` name the part.
* State stays on the facade. The run registry, the locks, the claims, the background
  task set, the cached dashboard state, the known models and the consolidation state
  are facade globals, so a ``global`` write in a part lands there and a test that
  rebinds one there is the binding every function sees.

The follow-up endpoints and ``register_routes`` stay in the facade:
``_require_enabled`` wraps the follow-up handlers when they are defined, and
composition rebinds a wrapper but not the handler it holds. Which part holds which
responsibility is recorded in the app's ``README.md`` (Layout).
"""
