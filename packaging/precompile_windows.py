"""Precompile the Windows desktop gateway's measured import closure.

The Windows installer ships a loose Python runtime.  Importing the gateway for
the first time otherwise creates more than a thousand small ``.pyc`` files
while antivirus scanning is hottest, turning a few seconds of Python work into
a long first launch.  This build helper imports the gateway without writing
bytecode, records only modules actually loaded from the bundled runtime, then
emits deterministic hash-based caches beside their sources.

Keeping this as a build helper (rather than runtime bootstrap code) means the
end user's first launch does no cache-population pass.  Hash-based pycs remain
valid when archive extraction changes mtimes, and relative ``co_filename``
values avoid leaking a CI runner path into tracebacks.

The caches are ``UNCHECKED_HASH``, and the distinction from ``CHECKED_HASH``
is worth stating because it is the difference between a fast launch and a slow
one.  What this tree needs from a hash-based pyc is *mtime independence*:
extraction restamps sources, so a TIMESTAMP pyc would look stale on every
user's machine and be rewritten in place, and that rewrite is the defect the
shipped caches exist to prevent.  Both hash modes deliver that.  They differ
only in whether the loader re-validates: a ``CHECKED_HASH`` pyc makes every
import read its ``.py`` in full and hash it *in addition to* reading the
``.pyc``.  Measured on native Windows over this closure, that second read costs
43.55 MB and ~3232 extra read operations per boot across 1639 additional cold
file opens.  The hashing arithmetic itself is free (~11 ms); the opens are not,
because each one pays a first-touch metadata and antivirus toll.  Cold cache --
the regime a real launch after login runs in -- that measured a median 16718 ms
with ``CHECKED_HASH`` against 4192 ms with ``UNCHECKED_HASH`` (n=5 per arm,
interleaved), a 12.5 s difference at the median and 10.9 s min-to-min, for
byte-identical code.  Warm it is worth 195 ms, which is why a warm benchmark
will conclude this change does nothing.

What ``UNCHECKED_HASH`` gives up is detecting a ``.py`` edited underneath the
cache: the stale bytecode is used instead.  That is the correct trade for a
runtime the installer owns and the user is not meant to edit, and it is not a
signing guarantee being dropped -- Authenticode seals the installer, not this
resource tree, so ``CHECKED_HASH`` was never what made the tree trustworthy.
Anyone deliberately editing the shipped runtime to debug it can force
validation back on for one run with ``--check-hash-based-pycs always``.
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
            invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
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
