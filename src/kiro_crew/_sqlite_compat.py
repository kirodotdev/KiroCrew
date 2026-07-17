"""SQLite import shim + FTS5 capability probe.

KiroCrew's memory and knowledge stores require SQLite's FTS5 full-text
extension. We prefer ``pysqlite3`` (which bundles a recent SQLite with FTS5)
when present, and fall back to the stdlib ``sqlite3``.

``pysqlite3-binary`` only ships wheels for Linux x86_64 (see setup.cfg), so on
macOS and Linux aarch64 we rely on the host's stdlib SQLite having FTS5 built
in. Modern macOS and mainstream Linux distros do, but minimal container images
occasionally compile SQLite without ``SQLITE_ENABLE_FTS5``. When that happens,
``CREATE VIRTUAL TABLE ... USING fts5`` raises ``no such module: fts5`` — and a
naive delete-and-retry self-heal loops on the same failure forever. This module
provides a one-shot probe so callers can fail loudly with an actionable message
instead.
"""

from __future__ import annotations

from functools import lru_cache

try:
    import pysqlite3 as sqlite3  # type: ignore
except ImportError:  # pragma: no cover - exercised on platforms without pysqlite3
    import sqlite3  # type: ignore

__all__ = ["sqlite3", "fts5_available", "FTS5_UNAVAILABLE_HINT", "require_fts5"]

FTS5_UNAVAILABLE_HINT = (
    "SQLite FTS5 full-text extension is not available in this Python's sqlite3 "
    "build. KiroCrew memory and knowledge search require it.\n"
    "  - Linux x86_64: pip install pysqlite3-binary (KiroCrew depends on it here).\n"
    "  - Linux aarch64 / minimal images: install a python3 whose libsqlite3 was "
    "built with SQLITE_ENABLE_FTS5, or `pip install pysqlite3-binary` if a wheel "
    "exists for your platform.\n"
    "  - macOS: the system Python and Homebrew Python both ship FTS5; reinstall "
    "Python from python.org or Homebrew if this fails."
)


@lru_cache(maxsize=1)
def fts5_available() -> bool:
    """Return True if the resolved sqlite3 module supports FTS5.

    Probes an in-memory database once and caches the result for the process.
    """
    try:
        conn = sqlite3.connect(":memory:")
    except Exception:  # pragma: no cover - sqlite itself broken
        return False
    try:
        conn.execute("CREATE VIRTUAL TABLE _fts5_probe USING fts5(x)")
        return True
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def require_fts5() -> None:
    """Raise RuntimeError with an actionable hint if FTS5 is unavailable."""
    if not fts5_available():
        raise RuntimeError(FTS5_UNAVAILABLE_HINT)
