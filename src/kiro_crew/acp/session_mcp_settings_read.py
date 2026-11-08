"""Why an MCP settings file could not be read, in words a user can act on.

The gated settings read in :mod:`kiro_crew.acp.session_mcp` refuses a file it
cannot read safely; the types and fixed reason texts here carry that refusal to
the message the user sees. :mod:`kiro_crew.acp.session_mcp` re-exports every
name, so callers keep that import path.
"""

from __future__ import annotations

import codecs
import errno
from pathlib import Path

from kiro_crew.acp.mcp_session_report import sanitize_sink_text

# How many characters of a settings file's PATH a refusal repeats. The path is
# the remedy -- it names the file the user opens -- so it is cut from the FRONT,
# keeping the tail that locates the file under its project. A project path is
# admitted, not typed, but its length is the filesystem's ceiling and not ours,
# and the string is retained per agent for the life of the runtime.
_NATIVE_NAMED_PATH_CHARS = 256


def _named_path(path: Path) -> str:
    """*path* as a message repeats it: its tail, within :data:`_NATIVE_NAMED_PATH_CHARS`.

    Cleaned like every other value the message carries: the path is the session's
    working directory as admitted, and a control character in it would reach the
    same log line.
    """
    text = str(path)
    text = sanitize_sink_text(text, len(text))
    if len(text) <= _NATIVE_NAMED_PATH_CHARS:
        return text
    return "..." + text[-(_NATIVE_NAMED_PATH_CHARS - 3) :]


#: Why a settings file could not be read, as :class:`NativeSettingsUnreadable`
#: repeats it to the user. Every reason is fixed text plus at most two
#: integers, so nothing from the file itself reaches the retained message.
_UNREADABLE_REFUSED_PATH = "its path was refused by the file-safety screen"
_UNREADABLE_UNOPENABLE = (
    "it could not be opened as a plain local file (a dangling link, a directory,"
    " a file this account cannot read, or a cloud placeholder)"
)
_UNREADABLE_TOO_LARGE = "it is larger than the safe read limit"
_UNREADABLE_NOT_UTF8 = "it is not UTF-8 text; re-save it as UTF-8"
_UNREADABLE_TOO_DEEP = "it nests deeper than the JSON limit"
_UNREADABLE_NOT_OBJECT = "it does not hold an object of servers"
_UNREADABLE_CHURN = "it was rewritten during every read"

#: Longest mark first: a UTF-32LE mark (FF FE 00 00) begins with the UTF-16LE one.
#: Not decoded: kiro-cli (2.34.0) refuses a UTF-16 or UTF-32 file too, so reading
#: one here would apply restrictions kiro-cli never loads.
_WIDE_BOMS = (
    (codecs.BOM_UTF32_LE, "UTF-32"),
    (codecs.BOM_UTF32_BE, "UTF-32"),
    (codecs.BOM_UTF16_LE, "UTF-16"),
    (codecs.BOM_UTF16_BE, "UTF-16"),
)


class SettingsReadRefusal(ValueError):
    """A settings read failed; ``reason`` says why, in words a user can act on."""

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def _unreadable_reason(exc: BaseException | None) -> str | None:
    """The fixed reason for a failed settings read, or ``None`` when unknown."""
    if isinstance(exc, SettingsReadRefusal):
        return exc.reason
    if isinstance(exc, OSError):
        name = errno.errorcode.get(exc.errno or 0)
        return f"it could not be opened ({name})" if name else "it could not be opened"
    return None


class NativeSettingsUnreadable(ValueError):
    """A settings file exists but could not be read safely.

    Raised by :func:`native_settings_sources` in place of the reader's own
    ``OSError``/``ValueError`` (kept as ``__cause__``) so a caller can name the
    file it could not read. It IS a ``ValueError``, so a caller that catches the
    reader's exceptions still catches this one.
    """

    def __init__(self, label: str, path: Path, reason: str | None = None) -> None:
        super().__init__(f"{label} ({_named_path(path)}) could not be read safely")
        self.label = label
        self.path = path
        #: Why, as fixed text (see :func:`_unreadable_reason`); ``None`` when unknown.
        self.reason = reason

    def explain(self, outcome: str) -> str:
        """One sentence for a user: why every native element is withheld."""
        because = f" because {self.reason}" if self.reason else ""
        return (
            f"{self.label} ({_named_path(self.path)}) could not be read safely{because},"
            f" so no Kiro Crew server can be mounted with this session's identity;"
            f" repair or remove that file to restore {outcome}"
        )
