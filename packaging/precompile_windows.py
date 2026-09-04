"""Precompile the Windows desktop gateway's measured import closure.

The Windows installer ships a loose Python runtime.  Importing the gateway for
the first time otherwise creates more than a thousand small ``.pyc`` files
while antivirus scanning is hottest, turning a few seconds of Python work into
a long first launch.  This build helper imports the gateway without writing
bytecode, records only modules actually loaded from the bundled runtime, then
emits deterministic checked-hash caches beside their sources.

Keeping this as a build helper (rather than runtime bootstrap code) means the
end user's first launch does no cache-population pass.  Checked-hash pycs remain
valid when archive extraction changes mtimes, and relative ``co_filename``
values avoid leaking a CI runner path into tracebacks.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import py_compile
import sys
from pathlib import Path
from types import ModuleType


def _module_source(module: ModuleType, root: Path) -> Path | None:
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str) or not raw.lower().endswith(".py"):
        return None
    source = Path(raw).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    return source if source.is_file() else None


def precompile_import_closure(root: Path, module_names: list[str]) -> tuple[int, int]:
    """Import ``module_names`` and compile loaded sources located under ``root``."""

    root = root.resolve(strict=True)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        importlib.invalidate_caches()
        for module_name in module_names:
            importlib.import_module(module_name)
    finally:
        sys.dont_write_bytecode = previous

    sources = {
        source
        for module in tuple(sys.modules.values())
        if module is not None and (source := _module_source(module, root)) is not None
    }
    if not sources:
        raise RuntimeError(f"no imported Python sources were found below {root}")

    total_bytes = 0
    for source in sorted(sources):
        cache = Path(importlib.util.cache_from_source(str(source)))
        relative_name = source.relative_to(root).as_posix()
        py_compile.compile(
            str(source),
            cfile=str(cache),
            dfile=relative_name,
            doraise=True,
            optimize=sys.flags.optimize,
            invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH,
        )
        total_bytes += cache.stat().st_size
    return len(sources), total_bytes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--module", action="append", required=True, dest="modules")
    args = parser.parse_args(argv)

    count, total_bytes = precompile_import_closure(args.root, args.modules)
    print(f"precompiled {count} startup modules ({total_bytes / 1024 / 1024:.1f} MiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
