"""The parts of the namespace launcher program, composed by ``kiro_crew.sandbox_launcher_program``.

The launcher is ONE stdlib-only program. :func:`kiro_crew.sandbox_launcher.render_namespace_launcher`
renders it with a spawn's plan, and the child runs that text as ``python -I -S`` with no package
to import. Its source is ``kiro_crew/sandbox_launcher_program.py`` plus the modules of this
package. The facade holds the program's head (its docstring, imports, constants, ``_load_libc``
and ``Launch``), the namespace entry and the stand-in root, and the run order (``place_masks``,
``run_child``, ``main``, the plan placeholder), with a marker line where each part's text goes.
Each part holds, below its own marker line, one run of the program's definitions.
:mod:`kiro_crew.sandbox_launcher` reads the facade and every part when it is imported, puts each
part's text at its marker and drops what follows the facade's end-of-program marker, so the text
a child runs is the program as one file, byte for byte. A file it cannot read, or a marker that
is missing, repeated or unknown, leaves it no program, and every Linux spawn then refuses.

Imported as a module, ``kiro_crew.sandbox_launcher_program`` binds every name its parts define,
and :func:`kiro_crew._facade_compose.compose` runs their functions on its globals, so the module
stays the one namespace the rendered program is. Tests drive the stages in-process and patch
them there -- the stages ``run_child`` calls, ``open``, ``_locked_mount_flags``,
``_stage_is_fresh_mount`` and the standard-library modules the facade imports -- and a patch
reaches every stage that reads the name. So:

* everything above a part's marker line is module glue the program does not carry: the part's
  docstring, the standard-library modules it reads, imported plainly, and the facade's names
  under ``TYPE_CHECKING`` for the type checker and the linter;
* below it a part holds definitions only: no module-level value, and nothing a ``def`` evaluates
  from the facade (a default, a decorator, an annotation), so a part imports without the facade
  and every name its functions read is the facade's;
* a function's ``__module__`` reads ``kiro_crew.sandbox_launcher_program``; only its source file
  names the part;
* the program's prose is the program's: a sentence in it that says "this module", "above" or
  "below" describes the program as one file, because its text stays byte for byte what the
  child runs;
* nothing but the facade imports a part, and the renderer reads the parts as text.

The parts, and where new work for each goes:

* :mod:`~kiro_crew.sandbox_launcher_parts.pinned_mounts` -- placing one hiding mount and proving
  where it landed
* :mod:`~kiro_crew.sandbox_launcher_parts.mask_stages` -- the stages that place the windows,
  seals and masks
* :mod:`~kiro_crew.sandbox_launcher_parts.exec_lockdown` -- what runs after the masks, up to the
  exec

``test/test_sandbox_launcher_program_parts.py`` pins all of this.
"""
