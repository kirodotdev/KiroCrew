"""Private owners composed by :mod:`kiro_crew.hooks`.

The hook subsystem's rules are split by responsibility across the modules of this
package, and ``kiro_crew.hooks`` stays their only import path and their only patch
surface: nothing but the facade imports an owner.

Each owner holds functions moved verbatim from the one-module file (``hook_types`` and
``script_hooks`` hold moved classes too), plus the definitions added since (the gate's tiers and ``GateFacts`` in ``gate_tiers``,
``ToolCall`` in ``tool_identity``). :func:`kiro_crew._facade_compose.compose`
makes every function an owner defines run on the facade module's globals rather than
its own. Over a hundred production modules import ``kiro_crew.hooks``, and the tests patch its
names -- ``hooks.MAX_FILE_BYTES``, ``.validate_file_path``, ``._fd_real_path``,
``.is_sensitive_path``, ``.os``, ``.run_script_hook``, ``.load_denied_commands_state``,
``.persisted_hook_store`` and dozens more. A function that read its owner's globals
would keep calling the unpatched object and the test would pass while exercising
nothing, so the hooks surface stays ONE namespace, as the one-module file was. Four
consequences follow:

* An owner's imports are inert for its functions. An owner imports the facade only
  under ``TYPE_CHECKING``, and stdlib names plainly; both serve the type checker and
  the linter. A ``@contextmanager``'s generator is not a function ``compose``
  rebinds, so ``hooks.py`` rebinds the one an owner holds
  (``ScriptHookStore._atomic_mutation``) with the shared helper's ``rebind`` right
  after ``compose``. Every name a function reads must exist in ``kiro_crew.hooks``, and
  ``test/test_hooks_composition_contract.py`` sweeps each function's bytecode to
  prove it does.
* A function's ``__module__`` reads ``kiro_crew.hooks`` as it did before the split,
  so reprs and pickling by reference are unchanged and it logs through that module's
  ``logger``. A moved class keeps its owner's ``__module__``, which ``compose`` reads to
  select the classes whose methods it rebinds. Its source file differs, so a log record's ``module``, ``filename`` and
  ``lineno`` name the owner file.
* State stays on the facade. Every module-level value -- the constants and event
  vocabularies, the deny-target grammar tables, the internal-read allowlists, the
  app and builtin-agent registries, the two UNC root memos, the global script-hook
  store, the ``_GATE_UNCOUNTED`` ContextVar -- is a ``kiro_crew.hooks`` global that
  owner functions reach by name, so a test that rebinds one there is the binding
  every function sees. No owner defines a module-level value, and a ``global``
  statement inside a rebound owner function writes the FACADE's namespace, which is
  what keeps ``set_builtin_app_names`` and ``set_global_hook_store`` working from an
  owner.
* The facade's own body runs BEFORE its ``compose`` call, so a facade-body call reaches an
  owner function that is not yet rebound. The two UNC root memos and their
  import-time priming therefore stay in the facade: priming an un-rebound owner
  function would write the memo into the owner's namespace and leave the facade's
  cache empty.

Which responsibility each owner holds, and which constructs stay in the facade
because repository guards read them there, is recorded in
``docs/system-specs/modules/memory-skills-hooks.md`` (the "Hook runtime owners"
section under Hooks).
"""
