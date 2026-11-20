"""Write pytest's assertion-rewrite cache for one CI file shard, in parallel.

Every xdist worker of a shard collects the whole shard, and on a fresh runner
each of them rewrites the asserts of every test file because no cached pyc
exists. This script produces those pycs once, across one process per core,
before pytest starts, so each worker reads a cached pyc instead.

It parses and compiles source text and never executes it: no test module,
``conftest.py`` or ``kiro_crew`` import happens here. The cache format, file
name and directory are pytest's own (``_rewrite_test``, ``_write_pyc``,
``PYC_TAIL`` and ``get_cache_dir`` from ``_pytest.assertion.rewrite``), so the
pyc always matches the installed pytest.

What is rewritten:

- the test files this shard owns: every file under ``testpaths`` whose name
  matches ``python_files``, kept when ``scripts.ci_file_shards.file_shard``
  names this shard as its owner (same hash, same pin file as the test step);
- every ``conftest.py`` under the rootdir and ``testpaths``. The rootdir
  ``conftest.py`` and ``test/conftest.py`` are loaded before the rootdir conftest
  redirects bytecode, so a process that starts without the redirect reads those
  two from the in-tree ``__pycache__`` and not from here; the conftests of
  deeper directories are loaded during collection, after the redirect.

Then it compiles the product modules: every ``*.py`` under the directories the ini's
``pythonpath`` names (``src``), to the ordinary import cache under the same root, with
``py_compile``. The rootdir conftest redirects bytecode to a root that is empty on a
fresh runner, so every worker would otherwise compile each product module it imports
while it collects, all workers the same ones at the same time: one shard's collection
imports about 3,900 of them. Third-party and standard-library modules are left to the
workers; a shard needs a sixth of the installed ones.

What is not: modules pytest rewrites only because something registered them at
import time (``pytest_plugins``, ``register_assert_rewrite``), and files a
conftest ``pytest_ignore_collect`` hook would drop. The first group is rewritten
by pytest as before; the second gets a pyc nobody reads. Neither changes which
files a shard collects: pytest's own discovery still decides that.

Best effort by contract: a file that cannot be rewritten is reported and left to
pytest, a run that exceeds its budget stops, and the exit status is 0 in every
case.

Run from the checkout root, as the user and in the directory the test step uses:
``python -m scripts.ci_rewrite_prewarm --file-shards N --file-shard K``.
"""

from __future__ import annotations

import argparse
import configparser
import fnmatch
import multiprocessing
import os
import sys
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path

from scripts.ci_file_shards import file_shard, read_pins

_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
# pytest's own ``norecursedirs`` default, plus the bytecode directory.
_DEFAULT_NORECURSEDIRS = (
    "*.egg",
    ".*",
    "_darcs",
    "build",
    "CVS",
    "dist",
    "node_modules",
    "venv",
    "{arch}",
)
_BUDGET_SECONDS = 240.0
#: Product modules that did not compile are named up to this many, then counted.
_REPORTED_UNCOMPILED = 5


def pycache_prefix() -> str | None:
    """The directory the suite's rootdir conftest sends bytecode to, or None.

    Mirrors ``_redirect_bytecode_cache`` in ``test/conftest_floor_configure.py``:
    ``$XDG_CACHE_HOME``, else ``.cache`` under the account's real home, then
    ``kirocrew/pycache``; nothing when bytecode writing is off.
    ``test/test_ci_rewrite_prewarm.py`` pins the two against each other.
    """
    if os.environ.get("PYTHONDONTWRITEBYTECODE"):
        return None
    cache_root = os.environ.get("XDG_CACHE_HOME")
    if not cache_root:
        home = None
        if os.name != "nt":
            try:
                import pwd

                home = pwd.getpwuid(os.getuid()).pw_dir
            except (ImportError, KeyError, OSError):
                home = None
        cache_root = os.path.join(home or str(Path.home()), ".cache")
    return os.path.join(cache_root, "kirocrew", "pycache")


def _pytest_ini(root: Path) -> Mapping[str, str]:
    """The ``[tool:pytest]`` section of the rootdir ``setup.cfg``, or nothing."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(root / "setup.cfg", encoding="utf-8")
    except (configparser.Error, OSError):
        return {}
    return dict(parser["tool:pytest"]) if parser.has_section("tool:pytest") else {}


def _words(value: str | None, default: Iterable[str]) -> tuple[str, ...]:
    """Split an ini list value on whitespace, dropping ``#`` comment lines."""
    if value is None:
        return tuple(default)
    lines = (line for line in value.splitlines() if not line.lstrip().startswith("#"))
    return tuple(word for line in lines for word in line.split())


def assertion_pass_hook_enabled(root: Path) -> bool:
    """Whether the ini turns on a rewrite variant this script does not produce."""
    value = _pytest_ini(root).get("enable_assertion_pass_hook", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


def discover(root: Path) -> tuple[list[Path], list[Path]]:
    """Return (test files, conftest files) under the rootdir's ``testpaths``.

    Reads file names only. A superset of what pytest collects is fine (a
    conftest ignore hook is not consulted); a subset only leaves work to pytest.
    """
    ini = _pytest_ini(root)
    patterns = _words(ini.get("python_files"), _DEFAULT_PYTHON_FILES)
    skip = (*_words(ini.get("norecursedirs"), _DEFAULT_NORECURSEDIRS), "__pycache__")
    tests: list[Path] = []
    conftests: list[Path] = []
    if (root / "conftest.py").is_file():
        conftests.append(root / "conftest.py")
    for testpath in _words(ini.get("testpaths"), (".",)):
        for directory, subdirs, names in os.walk(root / testpath):
            subdirs[:] = sorted(
                name
                for name in subdirs
                if not any(fnmatch.fnmatch(name, pattern) for pattern in skip)
                and not os.path.isfile(os.path.join(directory, name, "pyvenv.cfg"))
            )
            for name in sorted(names):
                path = Path(directory, name)
                if name == "conftest.py":
                    if path not in conftests:
                        conftests.append(path)
                elif any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
                    tests.append(path)
    return tests, conftests


def shard_files(
    root: Path, count: int, index: int, pins: Mapping[str, int] | None = None
) -> list[Path]:
    """The files to rewrite for one shard: its own test files, then every conftest."""
    tests, conftests = discover(root)
    owned = [path for path in tests if file_shard(path, root, count, pins) == index]
    return owned + conftests


class _Trace:
    """Stands in for pytest's ``AssertionState``: ``_write_pyc`` only calls ``trace``."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def trace(self, message: str) -> None:
        self.messages.append(message)


def _use_prefix(prefix: str | None) -> None:
    """Point this process at the cache root pytest will read (pool initializer)."""
    sys.pycache_prefix = prefix


def rewrite_file(source: str) -> str | None:
    """Rewrite one file's asserts and cache the result; return why not, or None.

    Never raises: a syntax error, an unreadable file or an unwritable cache
    directory all leave the file for pytest to rewrite itself.
    """
    try:
        from _pytest.assertion import rewrite

        path = Path(source)
        cache_dir = rewrite.get_cache_dir(path)
        if not rewrite.try_makedirs(cache_dir):
            return f"cannot create {cache_dir}"
        # No Config: rewrite_asserts takes None and then rewrites without the
        # assertion-pass hook, which is the variant the ini selects (checked in _run).
        source_stat, code = rewrite._rewrite_test(path, None)  # type: ignore[arg-type]
        state = _Trace()
        pyc = cache_dir / (path.name[:-3] + rewrite.PYC_TAIL)
        if not rewrite._write_pyc(state, code, source_stat, pyc):  # type: ignore[arg-type]
            return "; ".join(state.messages) or f"cannot write {pyc}"
    except Exception as exc:  # noqa: BLE001 -- best effort: pytest rewrites this file
        return f"{type(exc).__name__}: {exc}"
    return None


def _rewrite_reporting(source: str) -> tuple[str, str | None]:
    return source, rewrite_file(source)


def product_files(root: Path) -> list[Path]:
    """Every ``*.py`` under the directories the ini's ``pythonpath`` puts on ``sys.path``.

    Reads file names only. A module nothing imports gets a pyc nobody reads.
    """
    files: list[Path] = []
    for entry in _words(_pytest_ini(root).get("pythonpath"), ()):
        top = root / entry
        if not top.is_dir():
            continue
        for directory, subdirs, names in os.walk(top):
            subdirs[:] = sorted(name for name in subdirs if name != "__pycache__")
            files += [Path(directory, name) for name in sorted(names) if name.endswith(".py")]
    return files


def compile_file(source: str) -> str | None:
    """Compile one module to the import cache; return why not, or None.

    The pyc is the one ``import`` writes: same path under ``sys.pycache_prefix``, same
    invalidation mode. Never raises: a file that does not compile, or a cache directory
    that cannot be written, leaves the module for the importing process.
    """
    try:
        import py_compile

        py_compile.compile(source, doraise=True)
    except Exception as exc:  # noqa: BLE001 -- best effort: the importer compiles this file
        return f"{type(exc).__name__}: {exc}".splitlines()[0]
    return None


def _compile_reporting(source: str) -> tuple[str, str | None]:
    return source, compile_file(source)


def prewarm(
    files: list[Path],
    prefix: str | None,
    jobs: int,
    budget: float = _BUDGET_SECONDS,
    work: Callable[[str], tuple[str, str | None]] = _rewrite_reporting,
) -> tuple[int, list[tuple[str, str]], bool]:
    """Run ``work`` on ``files`` across ``jobs`` processes; by default, rewrite them.

    ``work`` takes a path and returns it with why it was not cached, or None.
    Returns (files cached, [(file, reason)] skipped, whether the budget ran out).
    Largest files go first so the processes finish close together. ``spawn`` on
    every OS: the children start from a clean interpreter, as on Windows. One
    job runs in this process, with no pool to start.
    """
    ordered = sorted(files, key=lambda path: (-_size(path), str(path)))
    sources = [str(path) for path in ordered]
    written = 0
    skipped: list[tuple[str, str]] = []
    if not sources:
        return written, skipped, False
    deadline = time.monotonic() + budget
    if min(jobs, len(sources)) <= 1:
        previous = sys.pycache_prefix
        _use_prefix(prefix)
        try:
            for source in sources:
                if time.monotonic() >= deadline:
                    return written, skipped, True
                reason = work(source)[1]
                if reason is None:
                    written += 1
                else:
                    skipped.append((source, reason))
        finally:
            _use_prefix(previous)
        return written, skipped, False
    context = multiprocessing.get_context("spawn")
    pool = context.Pool(min(jobs, len(sources)), _use_prefix, (prefix,))
    try:
        # One file per task: only the unchunked iterator takes a timeout.
        results = pool.imap_unordered(work, sources)
        for _ in sources:
            try:
                source, reason = results.next(timeout=max(0.0, deadline - time.monotonic()))
            except multiprocessing.TimeoutError:
                return written, skipped, True
            if reason is None:
                written += 1
            else:
                skipped.append((source, reason))
    finally:
        pool.terminate()
        pool.join()
    return written, skipped, False


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _cores() -> int:
    try:
        return len(os.sched_getaffinity(0))  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        return os.cpu_count() or 1


def _run(argv: list[str] | None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--file-shards", type=int, required=True)
    parser.add_argument("--file-shard", type=int, required=True)
    parser.add_argument("--file-shard-pins", default=None)
    parser.add_argument("--jobs", type=int, default=None, help="processes; default: one per core")
    parser.add_argument(
        "--pycache-prefix",
        default=None,
        help="cache root; default: the one the rootdir conftest sets",
    )
    parser.add_argument("--budget-seconds", type=float, default=_BUDGET_SECONDS)
    args = parser.parse_args(argv)
    started = time.perf_counter()
    label = f"rewrite prewarm (shard {args.file_shard}/{args.file_shards})"
    # The rootdir is where pytest is started, spelled as the OS reports it:
    # pytest builds module paths from the same string, and the cache
    # directory is derived from that path.
    root = Path(os.getcwd())
    if not 1 <= args.file_shard <= args.file_shards:
        print(f"{label}: skipped, shard index out of range")
        return
    if args.pycache_prefix is None and os.environ.get("PYTHONDONTWRITEBYTECODE"):
        print(f"{label}: skipped, PYTHONDONTWRITEBYTECODE is set so pytest reads no cache")
        return
    if assertion_pass_hook_enabled(root):
        print(f"{label}: skipped, enable_assertion_pass_hook changes the rewrite")
        return
    prefix = args.pycache_prefix or pycache_prefix()
    pins = read_pins(Path(args.file_shard_pins), args.file_shards) if args.file_shard_pins else {}
    files = shard_files(root, args.file_shards, args.file_shard, pins)
    jobs = args.jobs or _cores()
    written, skipped, timed_out = prewarm(files, prefix, jobs, args.budget_seconds)
    for source, reason in skipped:
        print(f"{label}: left to pytest: {source}: {reason}")
    if timed_out:
        print(f"{label}: stopped at the {args.budget_seconds:.0f} s budget; pytest does the rest")
    elapsed = time.perf_counter() - started
    print(
        f"{label}: {written} of {len(files)} files cached, {len(skipped)} left to pytest, "
        f"{jobs} processes, elapsed {elapsed:.1f} s, cache {prefix}"
    )
    modules = product_files(root)
    if not modules or timed_out:
        return
    compiled, uncompiled, timed_out = prewarm(
        modules, prefix, jobs, args.budget_seconds - elapsed, _compile_reporting
    )
    for source, reason in uncompiled[:_REPORTED_UNCOMPILED]:
        print(f"{label}: left to the importer: {source}: {reason}")
    if len(uncompiled) > _REPORTED_UNCOMPILED:
        print(f"{label}: and {len(uncompiled) - _REPORTED_UNCOMPILED} more left to the importer")
    if timed_out:
        print(f"{label}: stopped at the {args.budget_seconds:.0f} s budget; importers do the rest")
    print(
        f"{label}: {compiled} of {len(modules)} product modules compiled, "
        f"{len(uncompiled)} left to the importer, "
        f"elapsed {time.perf_counter() - started - elapsed:.1f} s"
    )


def main(argv: list[str] | None = None) -> int:
    """Always 0: this step saves time or does nothing, it never fails a job."""
    try:
        # A path the console encoding cannot spell is printed escaped.
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="backslashreplace")
        _run(argv)
    except SystemExit as exc:  # argparse usage error
        print(f"rewrite prewarm: skipped, bad arguments (exit {exc.code})")
    except Exception as exc:  # noqa: BLE001 -- best effort by contract
        print(f"rewrite prewarm: skipped, {type(exc).__name__}: {exc}")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
