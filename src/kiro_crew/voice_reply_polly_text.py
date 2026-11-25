"""How the Polly ``--text`` value reaches the ``aws`` CLI.

On Windows, ``CreateProcess`` runs a ``.cmd`` or ``.bat`` launcher (AWS CLI v1
installs ``aws.cmd``) through ``cmd.exe``, which re-parses the command line: a
double quote, ``|``, ``^`` or ``%`` inside the SSML is read as syntax, not text.
For such a launcher the SSML goes into a temp file and the CLI loads it through
``file://``, so no model-derived text is on the line ``cmd.exe`` parses. Every
other launcher, and every other platform, keeps the SSML in the argv as before.
"""

from __future__ import annotations

import os
import tempfile

from kiro_crew.platform_compat import IS_WINDOWS
from kiro_crew.sandbox import shielded_prepare_off_loop

_REPARSED_LAUNCHER_SUFFIXES = (".cmd", ".bat")
# AWS CLI v1 opens a ``file://`` path through ``os.path.expandvars``, which on
# Windows expands ``$NAME``, ``${NAME}`` and ``%NAME%``.
_EXPANDED_PATH_CHARACTERS = ("$", "%")


def polly_text_args(aws_bin: str, text: str) -> tuple[list[str], str | None]:
    """Return the ``--text`` argv pair, and the temp file the caller must delete.

    The file is UTF-8 without a BOM. AWS CLI v1 decodes a ``file://`` parameter
    with ``locale.getpreferredencoding()``, which answers UTF-8 because the child
    inherits the ``PYTHONUTF8=1`` every entrypoint publishes
    (``platform_compat.ensure_utf8_console``). Plain text (not ``<speak``) stays
    in the argv.
    """
    unchanged: tuple[list[str], str | None] = (["--text", text], None)
    batch = IS_WINDOWS and aws_bin.lower().endswith(_REPARSED_LAUNCHER_SUFFIXES)
    if not batch or not text.startswith("<speak"):
        return unchanged
    # A temp folder whose path the CLI would expand keeps main's argv.
    if any(ch in tempfile.gettempdir() for ch in _EXPANDED_PATH_CHARACTERS):
        return unchanged
    fd, path = tempfile.mkstemp(suffix=".ssml")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            # A lone surrogate has no UTF-8 form; it is spoken as "?" instead.
            f.write(text.encode("utf-8", "replace"))
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return ["--text", f"file://{path}"], path


async def polly_text_args_async(aws_bin: str, text: str) -> tuple[list[str], str | None]:
    """:func:`polly_text_args` with the temp-file work off the event loop.

    The hop is shielded: when the caller is cancelled while the worker is still
    writing, the file it creates is removed before the cancellation propagates.
    """

    def prepare() -> tuple[list[str], dict[str, str], str | None]:
        args, path = polly_text_args(aws_bin, text)
        return args, {}, path

    args, _unused_env, path = await shielded_prepare_off_loop(prepare)
    return args, path
