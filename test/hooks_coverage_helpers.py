"""Helpers shared by the ``kiro_crew.hooks`` coverage tests.

``test_hooks_coverage.py`` and its ``_internal_reads``, ``_hook_manager`` and
``_file_branches`` siblings import these by name: the ``_IS_WINDOWS`` flag, a byte-exact
file writer, a path comparison that resolves both sides (the Windows 8.3 short form), and
the marker that ``safe_write_file_nolink`` has staged its temp file.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"


# ── helpers ──


def _write(path: Path, text: str) -> Path:
    """Write *text* verbatim.

    ``newline="\\n"`` is explicit: the default translates ``\\n`` to ``\\r\\n``
    on Windows, which breaks the byte-exact assertions below.
    """
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def _same(a: str, b: str) -> bool:
    """Compare two paths after resolving BOTH sides.

    ``tempfile`` hands back the Windows 8.3 SHORT form while ``realpath``
    returns the long one, so a raw string compare passes on POSIX and fails
    only on Windows.
    """

    def _normalized(path: str) -> str:
        return os.path.normcase(os.path.normpath(os.path.realpath(path)))

    return _normalized(a) == _normalized(b)


def _staged_sibling(directory: Path, base: str) -> bool:
    """True once ``safe_write_file_nolink`` has staged its temp file for *base*.

    A deterministic marker for "the payload is written and the identity
    re-checks are next", used instead of counting ``os.stat`` calls -- the
    screening before staging varies with what the process has already cached.
    ``Path.iterdir`` uses ``scandir``, so it does not re-enter a patched
    ``os.stat``.
    """
    prefix = f".{base}.kirocrew-"
    return any(p.name.startswith(prefix) for p in directory.iterdir())
