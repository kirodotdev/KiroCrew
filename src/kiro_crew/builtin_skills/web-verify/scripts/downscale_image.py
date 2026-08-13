#!/usr/bin/env python3
"""Downscale an image so an agent can read it back without wedging the session.

A provider rejects the ENTIRE request when a many-image conversation carries any
image wider or taller than ``MAX_EDGE_PX``. kiro-cli replays the whole message
history every turn, and the offending block sits at a fixed history index that
nothing can evict — so one oversized read poisons every later turn, not just its
own. Capping is therefore not cosmetic, and the error is asymmetric: downscaling
costs some detail, skipping it costs the rest of the conversation.

This exists as a FILE rather than a shell one-liner in the skill because the
one-liner had a portability hole per platform — GNU-only ``readlink -f`` on
macOS, no ``python3`` on native Windows, and a path containing an apostrophe
breaking an inlined ``p='...'`` literal. Paths arrive as ``argv`` here, so
quoting is the shell's problem and not the snippet's.

Usage:

    python3 downscale_image.py shot.png [more.png ...]

Exit codes: 0 = every path is now within the cap (including ones already
within it), 1 = at least one path could not be processed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL import Image
else:
    # Optional at import time, not inside the function: a missing Pillow is a
    # RECOVERABLE state here (main re-execs under Kiro Crew's venv, which has
    # it), so the module must still import under an interpreter without it.
    try:
        from PIL import Image
    except ImportError:
        Image = None

#: Longest edge (px) any image may have. Mirrors ``MAX_IMAGE_EDGE_PX`` in
#: ``kiro_crew/acp/prompt_blocks.py``, which enforces the same ceiling on images
#: entering a prompt. Kept as a literal rather than an import: this script runs
#: under whichever interpreter has Pillow, which is not necessarily one that can
#: import ``kiro_crew``.
MAX_EDGE_PX = 2000

#: Set on the child when this script re-execs itself under a different
#: interpreter, so a Pillow-less environment cannot loop forever.
_REEXEC_ENV = "KIROCREW_DOWNSCALE_REEXEC"

#: Pixel ceiling for a decode. Pillow's stock guard is ~89M pixels, which a
#: legitimate `fullPage` capture of a long page clears easily -- 3000x60000 is
#: 180M -- and this script exists precisely to fix those. Raising it is safe for
#: this input class (the user's own screenshot, not untrusted upload), and the
#: `DecompressionBombError` handler below still fails a single path gracefully
#: rather than aborting the whole run when a file is beyond even this.
MAX_DECODE_PIXELS = 512_000_000


def bundled_python() -> str | None:
    """Path to an interpreter that has Pillow, or ``None``.

    Kiro Crew's own venv ships Pillow (``prompt_blocks`` depends on it), and the
    ``kirocrew`` console script lives in that venv's script directory — so the
    interpreter is its SIBLING. That holds on every platform without special
    casing the directory name: POSIX puts both in ``bin/``, Windows puts both in
    ``Scripts/``. Only the executable's suffix differs.

    ``realpath`` matters: the launcher is frequently a symlink from
    ``~/.local/bin`` into the venv, and the sibling lookup has to run in the
    venv, not next to the symlink.
    """
    launcher = shutil.which("kirocrew")
    if not launcher:
        return None
    script_dir = os.path.dirname(os.path.realpath(launcher))
    candidate = os.path.join(script_dir, "python.exe" if os.name == "nt" else "python")
    return candidate if os.path.isfile(candidate) else None


def _reexec_with_pillow(argv: list[str]) -> int:
    """Re-run this script under an interpreter that has Pillow."""
    if os.environ.get(_REEXEC_ENV):
        print(
            "downscale: Pillow is unavailable in "
            f"{sys.executable} and the re-exec already happened; install Pillow "
            "or run this under Kiro Crew's venv interpreter.",
            file=sys.stderr,
        )
        return 1
    interpreter = bundled_python()
    if not interpreter:
        print(
            "downscale: Pillow is unavailable and no Kiro Crew venv interpreter "
            "was found next to the 'kirocrew' launcher. Install Pillow into "
            f"{sys.executable}, or pass the image through another resizer.",
            file=sys.stderr,
        )
        return 1
    env = {**os.environ, _REEXEC_ENV: "1"}
    return subprocess.call([interpreter, os.path.abspath(__file__), *argv], env=env)


def shrink(path: str) -> tuple[bool, str]:
    """Cap ``path`` at ``MAX_EDGE_PX``. Returns ``(ok, message)``.

    Only rewrites a file actually over the cap, so running this over a directory
    is idempotent and never re-encodes a frame that was already safe
    (re-encoding a JPEG twice visibly degrades it).

    The rewrite goes to a sibling temp file and then ``os.replace``, NOT straight
    back over ``path``. Two reasons, and the first is a hard failure rather than a
    nicety: Pillow reads lazily and keeps the source file open, and on Windows a
    second handle opened for writing on the same path is refused
    (``PermissionError``) — which is exactly how this failed the Windows CI shard
    while passing on POSIX. The replace is also atomic, so an interrupted save
    cannot leave a truncated image where a valid one used to be.
    """
    assert Image is not None  # main() re-execs before reaching here

    # Raised only for the duration of this call and then put back: the ceiling
    # is a MODULE-LEVEL global in Pillow, so leaving it changed would silently
    # relax the guard for every later decode in the process.
    previous_ceiling = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS

    tmp: str | None = None
    try:
        with Image.open(path) as img:
            before = img.size
            if max(before) <= MAX_EDGE_PX:
                return True, f"{path}: {before[0]}x{before[1]} already within {MAX_EDGE_PX}px"
            fmt = img.format
            img.thumbnail((MAX_EDGE_PX, MAX_EDGE_PX))
            after = img.size
            fd, tmp = tempfile.mkstemp(
                dir=os.path.dirname(os.path.abspath(path)), suffix=".downscale-tmp"
            )
            os.close(fd)
            img.save(tmp, format=fmt)
        # OUTSIDE the with: the source handle must be closed before the replace,
        # or Windows refuses to overwrite the file it still has open.
        os.replace(tmp, path)
        tmp = None
    except FileNotFoundError:
        return False, f"{path}: no such file"
    except Image.DecompressionBombError as exc:
        # NOT an OSError, so it would otherwise escape main() and abandon every
        # remaining path -- the opposite of this script's job.
        return False, f"{path}: refusing to decode ({exc})"
    except OSError as exc:  # unreadable, or a format Pillow cannot decode
        return False, f"{path}: {exc}"
    finally:
        Image.MAX_IMAGE_PIXELS = previous_ceiling
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
    return True, f"{path}: {before[0]}x{before[1]} -> {after[0]}x{after[1]}"


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 1
    if Image is None:
        return _reexec_with_pillow(argv)

    failed = False
    for path in argv:
        ok, message = shrink(path)
        print(message, file=sys.stdout if ok else sys.stderr)
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
