"""Launch shim for app processes with gateway-provisioned dependencies.

Usage (always via the gateway's own interpreter)::

    python -m kiro_crew.apps.deps_boot <deps_dir> <script.py> [args...]
    python -m kiro_crew.apps.deps_boot <deps_dir> -m <module> [args...]
    python -m kiro_crew.apps.deps_boot <deps_dir> -c <code> [args...]

Why this exists: provisioned dependencies are a ``pip install --target``
tree, and the naive transport - prepending the tree to ``PYTHONPATH`` -
never processes ``.pth`` files, because ``PYTHONPATH`` entries are not site
directories. Packages that ship a ``.pth`` (editable installs, namespace
shims, import hooks) then install "successfully" and crash at import time.
``site.addsitedir`` IS the .pth-processing registration, so the shim runs it
on the deps dir and only then hands control to the real entry point.

``addsitedir`` appends; the app's pinned requirements must win over the
gateway environment's own packages, so the deps dir is placed at its FINAL
front position BEFORE ``addsitedir`` runs. Order matters at that moment,
not merely afterwards: a ``.pth`` line can ``import`` during site
processing, and whatever that import resolves to is cached in
``sys.modules`` - a reordering after the fact cannot evict a wrong module
cached under the old order. Entries the ``.pth`` files add are then moved
up next to the deps dir (their imports, when any, target finder modules
living in the deps dir itself, which is already correctly placed). The
shim adds no other behavior: argv is rewritten so the target sees exactly
the argv it would have seen launched directly.
"""

from __future__ import annotations

import os
import runpy
import site
import sys
import types
import zipfile


def main(argv: list[str]) -> None:
    # Path-launch support (`python -S /abs/.../deps_boot.py ...`, used when
    # import-machinery flags forbid the -m spelling): CPython puts the
    # SCRIPT's own directory at sys.path[0], which here is kiro_crew/apps/ -
    # leaving it would let an app import gateway modules by their bare names
    # (`import interpreter`). Drop it before anything else resolves.
    _own = os.path.dirname(os.path.abspath(__file__))
    if sys.path and os.path.abspath(sys.path[0] or os.curdir) == _own:
        del sys.path[0]
    if len(argv) < 2 or (argv[1] in ("-m", "-c") and len(argv) < 3):
        sys.stderr.write(
            "usage: python -m kiro_crew.apps.deps_boot <deps_dir> "
            "(<script.py> | -m <module> | -c <code>) [args...]\n"
        )
        raise SystemExit(2)
    deps_dir = argv[0]
    # CPython-parity placement, established BEFORE site processing: the
    # plain launch puts the LAUNCH ENTRY (script dir / "" for -c / cwd for
    # -m) at sys.path[0] and PYTHONPATH entries after it - so the deps go
    # AFTER the launch entry, or an app-local module colliding with a
    # dependency name would resolve to the DEPENDENCY under the shim while
    # resolving to the app's own file in a plain launch. Under -P/-I
    # (sys.flags.safe_path) the plain launch inserts NO launch entry at
    # all - the shim must not restore what those flags exist to remove.
    # The order must be final before addsitedir runs: a .pth import line
    # executes DURING site processing and caches whatever it resolves in
    # sys.modules, beyond the reach of any later reordering.
    safe_path = bool(getattr(sys.flags, "safe_path", False))
    if argv[1] == "-m":
        launch = [] if safe_path else [os.getcwd()]
    elif argv[1] == "-c":
        launch = [] if safe_path else [""]
    else:
        launch = [] if safe_path else [os.path.dirname(os.path.abspath(argv[1]))]
    deps_entry = os.path.abspath(deps_dir)
    sys.path[:0] = [*launch, deps_entry]
    _anchor = len(launch) + 1
    before = len(sys.path)
    # addsitedir dedups against entries already on sys.path (by normalized
    # abspath), so the pre-placed deps entry is not appended again; only
    # the .pth-added entries land at the tail. Those are moved up next to
    # the deps dir: their import lines, when any, target finder modules
    # living in the deps dir itself, already correctly placed - a plain
    # path line triggers no import at processing time.
    site.addsitedir(deps_entry)
    _pth_added = sys.path[before:]
    del sys.path[before:]
    sys.path[_anchor:_anchor] = _pth_added
    if argv[1] == "-m":
        module, rest = argv[2], argv[3:]
        sys.argv = [module, *rest]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    elif argv[1] == "-c":
        code, rest = argv[2], argv[3:]
        # `python -c` parity: sys.argv[0] is the literal "-c", and the code
        # runs in a REAL __main__ module registered in sys.modules with the
        # standard main-module globals (__spec__/__package__/__loader__ are
        # None for -c) - a bare dict would NameError on code that reads
        # __spec__ or pickles classes it defines.
        sys.argv = ["-c", *rest]
        mod = types.ModuleType("__main__")
        mod.__dict__.update(
            {
                "__name__": "__main__",
                "__doc__": None,
                "__package__": None,
                "__loader__": None,
                "__spec__": None,
                "__annotations__": {},
                "__builtins__": __builtins__,
            }
        )
        sys.modules["__main__"] = mod
        # The code string is the app manifest's own `-c` operand - exactly
        # what CPython itself would execute had the manifest launched
        # python directly; the shim adds no execution power it did not
        # already have.
        exec(  # noqa: S102  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            compile(code, "<string>", "exec"), mod.__dict__
        )
    elif os.name == "nt" and argv[1].lower().endswith(".exe"):
        # pip's EMBEDDED Windows launcher: a native exe with the console
        # script appended as a ZIP archive (zipfile reads it directly - the
        # central directory sits at EOF). Extract the __main__.py stub and
        # dispatch it here, after addsitedir, so its .pth-dependent imports
        # resolve - running the exe directly would skip site processing.
        # Windows-only BY THE GATE above: on POSIX a *.exe name is just a
        # python script like any other (only python targets ever reach the
        # shim), and it must take the script dispatch below - this arm's
        # execv fallback would restart its shebang interpreter WITHOUT the
        # deps path.
        target, rest = argv[1], argv[2:]
        if not zipfile.is_zipfile(target):
            # Not a launcher after all (the resolver gates on is_zipfile,
            # but the file can change between resolution and launch): a
            # native exe cannot be dispatched as python - hand the process
            # over to it directly, exactly as an unshimmed launch would.
            os.execv(target, [target, *rest])
        with zipfile.ZipFile(target) as zf:
            code = zf.read("__main__.py").decode("utf-8")
        sys.argv = [target, *rest]
        mod = types.ModuleType("__main__")
        mod.__dict__.update(
            {
                "__name__": "__main__",
                "__doc__": None,
                "__package__": None,
                "__loader__": None,
                "__spec__": None,
                "__annotations__": {},
                "__builtins__": __builtins__,
            }
        )
        sys.modules["__main__"] = mod
        # The stub is pip's own generated entry - the same code the exe
        # would have run had the manifest launched it directly.
        exec(  # noqa: S102  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected
            compile(code, target, "exec"), mod.__dict__
        )
    else:
        target, rest = argv[1], argv[2:]
        # Direct-script parity: `python script.py` puts the script's own
        # directory at sys.path[0] (established above, before site
        # processing); runpy.run_path does NOT, so a script importing a
        # sibling module would break under the shim.
        sys.argv = [target, *rest]
        runpy.run_path(target, run_name="__main__")


if __name__ == "__main__":
    main(sys.argv[1:])
