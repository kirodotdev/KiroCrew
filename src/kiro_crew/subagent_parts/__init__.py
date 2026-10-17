"""Owners composed by :mod:`kiro_crew.subagent`.

``kiro_crew.subagent`` is split by responsibility across the modules of this package and
stays its only import path and its only patch surface: nothing but ``kiro_crew.subagent``
imports a module of this package at runtime, except ``manager.py``, which imports the
``*_surface`` modules its class binds and ``run_limits`` for its default arguments.

Each owner holds definitions taken verbatim from the one-module file and keeps no state of
its own. :func:`kiro_crew._facade_compose.compose` makes every function an owner defines -- its module functions and
the methods of the classes it defines -- run on ``kiro_crew.subagent``'s module globals
rather than its own. Tests patch ``kiro_crew.subagent.sel``, ``.check_memory_available``,
``.open`` and some ninety more names; a function that read its owner's globals would keep
calling the unpatched object and the test would pass while exercising nothing, so the
subagent module stays ONE namespace, as the one-module file was, and as the
``subagent_manager`` coordinators already are: their ``*_impl`` methods are rebound onto the
same globals by :func:`kiro_crew.subagent_manager.bind_component_globals`, which reaches only
coordinator ``*_impl`` methods and so cannot rebind an owner's functions. Consequences:

* An owner's imports are inert for its functions. They name ``kiro_crew.subagent`` under
  ``TYPE_CHECKING``, for the type checker and the linter; every name a function reads must
  exist in ``kiro_crew.subagent``, and ``test/test_subagent_parts_composition_contract.py``
  sweeps every function's bytecode to prove it does.
* A value an owner computes while it is imported -- a constant, a default argument, a class
  body, a dataclass field factory -- reads the owner's own globals, so the owner imports what
  that needs for real, from the module that defines it.
* A function's ``__module__`` reads ``kiro_crew.subagent``, as before the split; only its
  source file differs. An owner's classes keep their own module, which is where ``inspect``
  looks for a class's source -- except ``SpawnApprovalUnreachable``, an exception callers
  catch by name, which reports ``kiro_crew.subagent`` and defines no function to rebind.
* A ``global`` statement in an owner function writes ``kiro_crew.subagent``'s namespace, so
  module state such a function rebinds is read there (``_host_read_in_flight``).

Which responsibility each owner holds, where new code goes, and which constructs stay in
``subagent.py`` because a repository guard reads them there, is recorded in
``docs/system-specs/modules/subagent.md`` (Composition).
"""
