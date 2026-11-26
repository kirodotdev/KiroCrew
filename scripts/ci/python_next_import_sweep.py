"""Compile every Python file and import every ``kiro_crew`` module.

Run by ``.github/workflows/python-next.yml`` on the next CPython the backend is
being brought up on. Two checks, both cheap next to the test suite:

1. Every ``.py`` under the given roots compiles, and compiling raises no
   ``SyntaxWarning``. Python 3.14 added one for ``return``, ``break`` and
   ``continue`` inside ``finally`` (PEP 765); a later release turns these into
   errors, so a new one is caught while it is still a warning.
2. Every module of the installed package that sits in a regular package (each
   directory down to it has an ``__init__.py``) imports. A removed stdlib API or
   a dependency without a wheel for this interpreter shows up here as one named
   module, before any test collects it. Directories without an ``__init__.py``
   (skill scripts, the vendored llama.cpp binding, an app's container image) are
   not import paths in a plain install, so step 1 is their only check.

Exit 0 when both pass, 1 otherwise, printing each failure on its own line.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import traceback
import warnings
from pathlib import Path

_SKIPPED_DIRS = {"__pycache__", "node_modules", "static"}


def _python_files(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SKIPPED_DIRS and not d.startswith(".")
        )
        found.extend(Path(dirpath, f) for f in sorted(filenames) if f.endswith(".py"))
    return found


def compile_failures(roots: list[Path]) -> list[str]:
    failures: list[str] = []
    for root in roots:
        for path in _python_files(root):
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                try:
                    compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
                except SyntaxError as exc:
                    failures.append(f"{path}:{exc.lineno}: SyntaxError: {exc.msg}")
            for warning in caught:
                failures.append(
                    f"{warning.filename}:{warning.lineno}: "
                    f"{warning.category.__name__}: {warning.message}"
                )
    return failures


def _module_names(package_dir: Path, package: str) -> list[str]:
    names: set[str] = set()
    for path in _python_files(package_dir):
        parts = path.relative_to(package_dir).with_suffix("").parts
        if not all(
            (package_dir.joinpath(*parts[:depth]) / "__init__.py").is_file()
            for depth in range(len(parts))
        ):
            continue
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join((package, *parts)))
    return sorted(names)


def import_failures(package: str) -> tuple[list[str], int]:
    root = importlib.import_module(package)
    package_dir = Path(root.__file__ or "").parent
    failures: list[str] = []
    imported = 0
    for name in _module_names(package_dir, package):
        try:
            importlib.import_module(name)
        except BaseException as exc:  # a SystemExit at import fails too
            frames = traceback.extract_tb(exc.__traceback__)
            where = f" ({frames[-1].filename}:{frames[-1].lineno})" if frames else ""
            failures.append(f"{name}: {type(exc).__name__}: {exc}{where}")
        else:
            imported += 1
    return failures, imported


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compile and import sweep for a new Python.")
    parser.add_argument("roots", nargs="+", type=Path, help="files or directories to compile")
    parser.add_argument("--package", default="kiro_crew", help="installed package to import")
    args = parser.parse_args(argv)

    compiled = compile_failures(args.roots)
    imported, ok = import_failures(args.package)
    gil = getattr(sys, "_is_gil_enabled", lambda: True)()
    print(f"Python {sys.version.split()[0]}, GIL enabled: {gil}")
    print(f"compile: {len(compiled)} failure(s)")
    print(f"import: {ok} module(s) imported, {len(imported)} failure(s)")
    for line in compiled + imported:
        print(f"FAIL {line}")
    return 1 if compiled or imported else 0


if __name__ == "__main__":
    sys.exit(main())
