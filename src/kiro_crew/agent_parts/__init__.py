"""Owners composed by :mod:`kiro_crew.agent`.

``kiro_crew.agent`` stays the import path and the patch target of everything here, and
nothing but ``kiro_crew.agent`` imports a module of this package.
:func:`kiro_crew._facade_compose.compose` makes every function a module here defines run
on ``kiro_crew.agent``'s module globals rather than its own. Tests patch
``agent.kiro_agents_dir_path``, ``agent._atomic_json_write``, ``agent.__file__`` and many
other names, and moved code rebinds module globals with ``global``; code that read its
owner's globals would miss both, so the module stays ONE namespace, as the one-module
file was. Consequences:

* An owner's imports are inert for its functions. It imports standard-library modules
  plainly and names ``kiro_crew.agent`` under ``TYPE_CHECKING``, for the type checker and
  the linter; every name a function reads must exist in ``kiro_crew.agent``, and
  ``test/test_agent_parts.py`` sweeps every function's bytecode to prove it does.
* A module-level value an owner defines (a prompt, a grant tuple, a function's state) is
  bound on ``kiro_crew.agent`` to the same object, so a test that rebinds it there
  rebinds what every function reads, and a ``global`` rebind lands there too.
* A function's ``__module__`` reads ``kiro_crew.agent``, it logs through that module's
  ``logger`` and reads its ``__file__``; only its source file differs, so
  ``inspect.getsource`` and a log record's ``filename`` name the owner file.
* Two functions are wrapped where they are defined: the rebuild's outcome tracker
  and the spec lock's context manager. A rebind reaches a wrapper but not the
  function its closure holds, so ``kiro_crew.agent`` wraps both again around a
  rebound copy.
* What stays in ``kiro_crew.agent``: the forwarding machinery, the managed MCP server table (its invocation lambdas
  read this namespace), the hook and config paths a test fixture pins by module, the
  bundled config directory (computed from ``__file__``), the skill-path scan, the
  dynamic-field refresh, the knowledge, research and heartbeat prompts, and the two
  exception classes.

The owners, and where new work for each goes:

* :mod:`~kiro_crew.agent_parts.kirocrew_launcher` -- the ``kirocrew`` launcher a spec runs, and first-run setup
* :mod:`~kiro_crew.agent_parts.shared_home` -- declining an agents home another install owns
* :mod:`~kiro_crew.agent_parts.spec_build` -- what a fresh spec is built from, and ``build_agent_config``
* :mod:`~kiro_crew.agent_parts.spec_files` -- how a spec file is written: the atomic write, the spec lock and the agents
  directory
* :mod:`~kiro_crew.agent_parts.spec_grants` -- the server grants each agent gets
* :mod:`~kiro_crew.agent_parts.spec_lookup` -- finding and reading a spec file
* :mod:`~kiro_crew.agent_parts.spec_prompts` -- the installed agents' system prompts
* :mod:`~kiro_crew.agent_parts.spec_rebuild` -- ``rebuild_agent_config``, what runs it, and keeping the specs' grants in
  step with the governance ceiling
* :mod:`~kiro_crew.agent_parts.spec_upgrades` -- migrations, model pins and the startup hook repair of the specs already on
  disk
"""
