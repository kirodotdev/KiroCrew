"""Name a JSON syntax error in ``config.json`` / ``config.local.json`` for a person.

The loader treats a file that does not parse as absent: it logs one WARNING per
load, marks the file degraded and runs on defaults, which leaves the file itself
untouched. That is the right recovery, but nothing told the person who saved the
broken file: the terminal and the dashboard both read "all systems running" while
every setting they had made was ignored. :func:`find_config_syntax_errors` reads
the two hand-edited files as the person sees them on disk and returns one
:class:`ConfigSyntaxError` per file that does not parse, carrying the path, the
line and column, and one concrete fix. The gateway prints it at start and posts it
as a bell note (``notifications/config_syntax_note.py``).

Read-only: the broken copy is never repaired or moved here. No text from the file is
quoted: the report reaches the terminal, the log and the bell, and the line that
fails to parse is the one most likely to hold a value in a shape no masking
recognises (unquoted, or half-quoted).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.config import loosening_provenance
from kiro_crew.config.loader_locations import config_local_path, config_path
from kiro_crew.user_json import strip_utf8_bom


def _read_regular_config_bytes(path: Path) -> bytes:
    """The bytes of *path*, refusing a *path* that is not a regular file.

    A name pointed at a FIFO or ``/dev/zero`` raises ``OSError`` here, as the
    loader's own read does, instead of blocking or never ending. An absent file
    raises ``FileNotFoundError``.
    """
    return loosening_provenance.read_regular_with_retry(path)


def _serves_endorsed_copy(leaf: str, disk: bytes) -> bool:
    """True when the loader parses the gateway-endorsed copy of *leaf*, not *disk*.

    Where the sandbox seals these files, a file that differs from its endorsed copy
    loads as that copy (``loosening_provenance.reconcile``), so a broken hand edit
    leaves the last approved settings in effect rather than defaults. A copy that
    cannot be read, or does not parse either, leaves the loader on its degraded path.
    Which bytes are served is the loader's own answer, asked of ``reconcile``.
    """
    try:
        served = loosening_provenance.reconcile(leaf, disk)
    except OSError:
        return False
    if served == disk:
        return False
    try:
        json.loads(strip_utf8_bom(served.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return True


def position_sentence(path: Path | str, line: int, column: int, message: str) -> str:
    """The one sentence that says where a config file stops parsing and the parser's reason."""
    return f"{path} has a JSON syntax error on line {line}, column {column}: {message}."


@dataclass(frozen=True)
class ConfigSyntaxError:
    """One config file that does not parse as JSON, and where."""

    path: Path
    line: int
    column: int
    message: str
    fix: str
    #: ``<path>.bak`` when one exists: the copy the last settings migration took,
    #: which can be older than the edit that broke the file.
    backup: Path | None = None
    #: True for ``config.local.json``: the loader keeps ``config.json`` and drops
    #: only the overlay, so its settings are not on defaults.
    overlay: bool = False
    #: True when the loader serves the gateway-endorsed copy instead of this file:
    #: the last approved settings are in effect and the edit waits for review.
    endorsed: bool = False

    def summary(self) -> str:
        """One line: the file, the position and the parser's reason."""
        return position_sentence(self.path, self.line, self.column, self.message)

    def explain(self) -> str:
        """The summary and the fix, for a terminal or a note body."""
        lines = [self.summary()]
        if self.endorsed:
            # A pending edit is applied only through Settings > Security; a restart
            # alone keeps serving the endorsed copy.
            lines.append(
                "Kiro Crew is ignoring this edit and running on the last settings it "
                "approved; it will not save over the file until you apply or discard the edit."
            )
            lines.append(
                f"To fix it: {self.fix}, save the file, then apply the change in "
                "Settings > Security (or run `kirocrew config pending`)."
            )
            self._append_backup(lines)
            return "\n".join(lines)
        # The gateway and dashboard writers refuse a file that does not parse (``save``
        # and ``read_config_for_update`` raise ConfigReadError). The interactive
        # ``config set --local`` (the overlay) and ``config set --file`` (config.json)
        # reset a corrupt file on purpose (``on_corrupt="reset"`` in ``cli_config``).
        if self.overlay:
            ignored = (
                "Kiro Crew is ignoring the overrides in this file and running on config.json alone"
            )
            resetter = "`kirocrew config set --local`"
        else:
            ignored = "Kiro Crew is ignoring this file and running on default settings"
            resetter = "`kirocrew config set --file`"
        lines.append(
            f"{ignored}; it will not save over the file until it parses again, "
            f"but {resetter} replaces it."
        )
        lines.append(f"To fix it: {self.fix}, save the file, then restart Kiro Crew.")
        self._append_backup(lines)
        return "\n".join(lines)

    def _append_backup(self, lines: list[str]) -> None:
        if self.backup is not None:
            lines.append(
                f"An earlier copy from a settings migration is at {self.backup}; "
                "it may be older than your last edit."
            )


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _column_of(text: str, pos: int) -> int:
    return pos - (text.rfind("\n", 0, pos) + 1) + 1


def _ends_its_line(text: str, pos: int) -> bool:
    """True when only whitespace follows *pos* up to the end of its line."""
    newline = text.find("\n", pos + 1)
    return not text[pos + 1 : newline if newline >= 0 else len(text)].strip()


def _previous_token(text: str, pos: int) -> int:
    """The index of the last non-whitespace character before *pos*, or -1."""
    i = min(pos, len(text)) - 1
    while i >= 0 and text[i].isspace():
        i -= 1
    return i


def _fix_for(text: str, err: json.JSONDecodeError) -> str:
    """One concrete edit for the commonest hand-edit mistakes, else a generic one.

    "At the end of line N" is said only when the token it is about ends that line;
    on a line that goes on (a one-line file), the edit is placed by its column. A
    comma is added only before a next value (not at the end of the file, where a
    bracket is missing, nor before a closing bracket that does not match), and
    removed only when a closing bracket follows it.
    """
    prev = _previous_token(text, err.pos)
    at = text[err.pos : err.pos + 1]
    if (
        err.msg.startswith("Expecting ',' delimiter")
        and prev >= 0
        and at.strip()
        and at not in "}]"
    ):
        if _ends_its_line(text, prev):
            return f"add a comma at the end of line {_line_of(text, prev)}"
        return f"add a comma on line {err.lineno} just before column {err.colno}"
    comma = -1
    if err.msg.startswith("Illegal trailing comma"):
        # Python 3.13+ names the trailing comma itself.
        comma = err.pos if at == "," else prev
    elif err.msg.startswith(("Expecting property name", "Expecting value")) and at in ("}", "]"):
        comma = prev
    if comma >= 0 and text[comma] == ",":
        if _ends_its_line(text, comma):
            return f"remove the comma at the end of line {_line_of(text, comma)}"
        return (
            f"remove the comma on line {_line_of(text, comma)} at column {_column_of(text, comma)}"
        )
    return f"correct the JSON on line {err.lineno} at column {err.colno}"


def syntax_error_in(path: Path, *, overlay: bool = False) -> ConfigSyntaxError | None:
    """The syntax error in the config file at *path*, or None when it parses or is absent.

    Read as the person sees it on disk, BOM dropped: the report is about the bytes
    they edited. Read through :func:`_read_regular_config_bytes`, so a name pointed at
    a FIFO or a device is refused instead of blocking or never ending. An absent or
    unreadable file is the loader's warning to give, not a syntax error, so it reads
    as None here.
    """
    try:
        disk = _read_regular_config_bytes(path)
        text = strip_utf8_bom(disk.decode("utf-8"))
    except (OSError, UnicodeDecodeError):
        return None
    try:
        json.loads(text)
    except json.JSONDecodeError as err:
        backup = Path(str(path) + ".bak")
        return ConfigSyntaxError(
            path=path,
            line=err.lineno,
            column=err.colno,
            message=err.msg,
            fix=_fix_for(text, err),
            backup=backup if backup.is_file() else None,
            overlay=overlay,
            endorsed=_serves_endorsed_copy(
                (
                    loosening_provenance.LEAF_CONFIG_LOCAL
                    if overlay
                    else loosening_provenance.LEAF_CONFIG
                ),
                disk,
            ),
        )
    except RecursionError:
        return None
    return None


def find_config_syntax_errors() -> list[ConfigSyntaxError]:
    """The syntax errors in ``config.json`` then ``config.local.json``. BLOCKING: reads files."""
    found = [
        syntax_error_in(config_path()),
        syntax_error_in(config_local_path(), overlay=True),
    ]
    return [err for err in found if err is not None]
