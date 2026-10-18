"""Parts composed by :mod:`kiro_crew.dashboard.chat_persistence`.

The dashboard slot save and restore orchestration is split by responsibility
across the modules of this package, and ``kiro_crew.dashboard.chat_persistence``
stays its only import path and its only patch surface: nothing but that module
imports a part.

Each part holds functions moved verbatim from the one-module file.
:func:`kiro_crew._facade_compose.compose` makes every function a part defines --
its module functions and the methods of the classes it defines -- run on the
facade's globals rather than its own. Tests patch the facade's names
(``chat_persistence.atomic_write``, ``._save_slot_to_history``,
``._reasoning_effort_values`` and dozens more), and the conftest fixtures reset
its process state there, so the save and restore code stays ONE namespace, as the
one-module file was. Three consequences follow:

* A part's imports are inert for its functions. A part imports the facade only
  under ``TYPE_CHECKING``, stdlib names plainly, and at run time only what a
  module-level statement evaluates when the part loads. Every name a function
  reads must exist in ``kiro_crew.dashboard.chat_persistence``, which binds every
  name a part defines; ``test_chat_persistence_parts_composition_contract.py``
  sweeps each function's bytecode to prove it.
* A function's ``__module__`` reads ``kiro_crew.dashboard.chat_persistence``, as it
  did before the split, and it logs through that module's ``logger``; its source
  file differs, so a log record's ``filename`` and ``lineno`` name the part. A
  part's class keeps its own module, which is where ``inspect`` looks for its
  source.
* State stays on the facade. The effort allowlist, the entry memo and every value
  a ``global`` statement rebinds are facade globals, so a ``global`` write in a
  part lands there and a test that rebinds one there is the binding every
  function sees.

The rules the orchestration consults are not here: they are the
``kiro_crew.dashboard.slot_persistence`` owners, which read the facade at call
time instead. Which part holds which responsibility is recorded in
``docs/system-specs/modules/history.md`` (Dashboard History Persistence).
"""
