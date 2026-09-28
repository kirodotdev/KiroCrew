"""Shared retention limits for the read-only refactor inventory helpers."""

from __future__ import annotations

import argparse
import sys
import unicodedata
from typing import Any

MAX_CLI_ITEMS = 256
MAX_RETAINED_ITEMS = 50_000
MAX_RETAINED_STRING_CHARS = 4_096
MAX_SOURCE_FILE_BYTES = 16 * 1024 * 1024


def bounded_cli_text(value: str) -> str:
    """Reject a CLI string that cannot fit in a retained report field."""

    if len(value) > MAX_RETAINED_STRING_CHARS:
        raise argparse.ArgumentTypeError(
            f"value exceeds the {MAX_RETAINED_STRING_CHARS}-character limit"
        )
    return value


class BoundedAppendAction(argparse.Action):
    """Append a bounded CLI value without retaining an unbounded option list."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        current = list(getattr(namespace, self.dest, None) or [])
        if len(current) >= MAX_CLI_ITEMS:
            parser.error(f"{option_string or self.dest} accepts at most {MAX_CLI_ITEMS} values")
        current.append(values)
        setattr(namespace, self.dest, current)


def filesystem_decode(value: bytes) -> str:
    """Decode an OS or Git pathname while preserving undecodable bytes."""

    return value.decode(sys.getfilesystemencoding(), errors="surrogateescape")


def bounded_text(value: str) -> tuple[str, bool]:
    """Return *value* within the shared string cap and whether it was truncated."""

    if len(value) <= MAX_RETAINED_STRING_CHARS:
        return value, False
    marker = "…[truncated]"
    return value[: MAX_RETAINED_STRING_CHARS - len(marker)] + marker, True


def markdown_code(value: str) -> str:
    """Render untrusted text as one control-free Markdown code span."""

    escaped: list[str] = []
    for character in value:
        codepoint = ord(character)
        if character in "`|" or unicodedata.category(character).startswith("C"):
            width = 4 if codepoint <= 0xFFFF else 8
            escaped.append(f"\\{'u' if width == 4 else 'U'}{codepoint:0{width}x}")
        else:
            escaped.append(character)
    return "`" + "".join(escaped) + "`"
