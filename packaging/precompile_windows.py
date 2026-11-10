"""Precompile a desktop backend's measured import closure.

Named for the lane it was written for; the macOS lane runs it too (see
"macOS" below).

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

The Windows caches are ``UNCHECKED_HASH``, and the distinction from
``CHECKED_HASH`` is worth stating because it is the difference between a fast
launch and a slow one.  What this tree needs from a hash-based pyc is *mtime
independence*: extraction restamps sources, so a TIMESTAMP pyc would look stale
on every user's machine and be rewritten in place, and that rewrite is the
defect the shipped caches exist to prevent.  Both hash modes deliver that.  They
differ only in whether the loader re-validates: a ``CHECKED_HASH`` pyc makes
every import read its ``.py`` in full and hash it *in addition to* reading the
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

macOS runs it with ``--invalidation-mode checked-hash``, then again with
``--verify``.  A cache for every module in the tree would put tens of thousands
of extra files under the code seal, and the signing service reads the whole
tree on every request, so the signed bundle carries caches for this closure
only and the bundled interpreter is barred from writing any other (see
``install_bytecode_write_ban``): a module outside the closure compiles in memory
and leaves no file behind.  ``--verify`` is the build's coverage gate: a fresh
process traces the closure again and fails on any module whose cache is
missing, was compiled in the other mode, or does not match its source.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import py_compile
import sys
from pathlib import Path
from types import ModuleType

INVALIDATION_MODES = {
    "unchecked-hash": py_compile.PycInvalidationMode.UNCHECKED_HASH,
    "checked-hash": py_compile.PycInvalidationMode.CHECKED_HASH,
}

# The flags word of a hash-based pyc header (PEP 552): bit 0 says the pyc is
# hash-based, bit 1 says the loader re-hashes the source before trusting it.
_PYC_FLAGS = {
    py_compile.PycInvalidationMode.UNCHECKED_HASH: 0b01,
    py_compile.PycInvalidationMode.CHECKED_HASH: 0b11,
}

# Sorted ahead of every other ``.pth`` in the directory: ``site`` runs them in
# name order, and one that imports a site-packages module before this line has
# run could still write that module's cache.
WRITE_BAN_PTH = "00-kirocrew-no-bytecode-writes.pth"
WRITE_BAN_LINE = "import sys; sys.dont_write_bytecode = True\n"


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


def trace_import_closure(root: Path, module_names: list[str]) -> set[Path]:
    """Import ``module_names`` without writing bytecode; return loaded sources under ``root``."""

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
    return sources


def precompile_import_closure(
    root: Path,
    module_names: list[str],
    invalidation_mode: py_compile.PycInvalidationMode = (
        py_compile.PycInvalidationMode.UNCHECKED_HASH
    ),
) -> tuple[int, int]:
    """Import ``module_names`` and compile loaded sources located under ``root``."""

    root = root.resolve(strict=True)
    sources = trace_import_closure(root, module_names)
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
            invalidation_mode=invalidation_mode,
        )
        total_bytes += cache.stat().st_size
    return len(sources), total_bytes


def _cache_problem(source: Path, invalidation_mode: py_compile.PycInvalidationMode) -> str | None:
    cache = Path(importlib.util.cache_from_source(str(source)))
    try:
        header = cache.read_bytes()[:16]
    except OSError:
        return "no cache"
    if len(header) < 16 or header[:4] != importlib.util.MAGIC_NUMBER:
        return "not a cache for this interpreter"
    flags = int.from_bytes(header[4:8], "little")
    if flags != _PYC_FLAGS[invalidation_mode]:
        return f"compiled with flags {flags:#04b}, expected {_PYC_FLAGS[invalidation_mode]:#04b}"
    if header[8:16] != importlib.util.source_hash(source.read_bytes()):
        return "compiled from different source bytes"
    return None


def uncovered_import_closure(
    root: Path,
    module_names: list[str],
    invalidation_mode: py_compile.PycInvalidationMode,
) -> list[str]:
    """Trace the closure again; name each module without a valid ``invalidation_mode`` cache."""

    root = root.resolve(strict=True)
    problems = []
    for source in sorted(trace_import_closure(root, module_names)):
        problem = _cache_problem(source, invalidation_mode)
        if problem is not None:
            problems.append(f"{source.relative_to(root).as_posix()}: {problem}")
    return problems


def install_bytecode_write_ban(site_packages: Path) -> Path:
    """Make every interpreter that loads ``site_packages`` refuse to write bytecode.

    ``PYTHONDONTWRITEBYTECODE`` is not enough on its own for a sealed bundle:
    it reaches only the processes that inherit the desktop shell's
    environment, and the agent runtime's own children (the MCP stubs and
    bridges it launches with this interpreter) run with the Python variables
    stripped. A ``.pth`` line is executed by ``site`` in every one of them.
    """

    target = site_packages / WRITE_BAN_PTH
    target.write_text(WRITE_BAN_LINE, encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--module", action="append", required=True, dest="modules")
    parser.add_argument(
        "--invalidation-mode",
        choices=sorted(INVALIDATION_MODES),
        default="unchecked-hash",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="check the closure's caches instead of writing them",
    )
    parser.add_argument(
        "--ban-bytecode-writes",
        type=Path,
        metavar="SITE_PACKAGES",
        help="after compiling, bar interpreters that load SITE_PACKAGES from writing bytecode",
    )
    args = parser.parse_args(argv)
    mode = INVALIDATION_MODES[args.invalidation_mode]

    if args.verify:
        problems = uncovered_import_closure(args.root, args.modules, mode)
        if problems:
            print(f"{len(problems)} traced module(s) lack a valid {args.invalidation_mode} cache:")
            for line in problems[:20]:
                print(f"  {line}")
            return 1
        print(f"every traced module has a valid {args.invalidation_mode} cache")
        return 0

    count, total_bytes = precompile_import_closure(args.root, args.modules, mode)
    print(f"precompiled {count} startup modules ({total_bytes / 1024 / 1024:.1f} MiB)")
    if args.ban_bytecode_writes is not None:
        print(f"bytecode writes barred by {install_bytecode_write_ban(args.ban_bytecode_writes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
