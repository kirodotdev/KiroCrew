"""_toolpath.py -- the one tool lookup the demo-recording reference scripts share.

The doctor (deps.py) and the pipeline scripts (narrate.py, verify_align.py) must
agree on where a tool such as ffmpeg or ffprobe lives, or a green doctor is
followed by a pipeline that cannot find the same binary. So both resolve through
``which_any`` here instead of each building its own path.

Every directory is probed with ``shutil.which(binary, path=dir)`` rather than by
testing ``dir / binary``. On Windows the file is ``ffprobe.exe``, and only
``shutil.which`` applies ``PATHEXT`` to find it; a bare-path test never matches.
On POSIX the result is the same as before: an executable regular file.

Imports nothing from kiro_crew, so deps.py can keep diagnosing an interpreter
that cannot import it.
"""

from __future__ import annotations

import pathlib
import shutil


def user_bin_dirs(home: pathlib.Path) -> list[pathlib.Path]:
    """Fallback directories searched when a tool is not on PATH.

    ~/.local/bin is the standard user bin dir. Anything more specific is one
    machine's layout and does not belong in a shipped skill. Takes the home
    directory as an argument so callers resolve it per call, never at import.
    """
    return [home / ".local" / "bin"]


def which_any(binary: str, extra_dirs: list[pathlib.Path]) -> str | None:
    """PATH first, then each directory in *extra_dirs*, in order."""
    found = shutil.which(binary)
    if found:
        return found
    for d in extra_dirs:
        found = shutil.which(binary, path=str(d))
        if found:
            return found
    return None
