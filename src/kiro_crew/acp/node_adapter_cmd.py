"""Resolve the stock npm Windows batch shim to its Node entry script."""

from __future__ import annotations

import re
from pathlib import Path

_NPM_NODE_LINE = re.compile(
    r'^\s*"%_prog%"\s+"%dp0%[\\/](node_modules[\\/][^"\r\n]+\.[cm]?js)"\s+%\*\s*$',
    re.IGNORECASE | re.MULTILINE,
)


def npm_cmd_entry(script: str) -> Path | None:
    """Recognize only the stock Node invocation; leave custom batch files alone."""
    shim = Path(script)
    if shim.suffix.lower() != ".cmd":
        return None
    try:
        if shim.stat().st_size > 65_536:
            return None
        matches = list(_NPM_NODE_LINE.finditer(shim.read_text("utf-8")))
        if len(matches) != 1:
            return None
        relative = re.split(r"[\\/]", matches[0].group(1))
        entry = shim.parent.joinpath(*relative).resolve(strict=True)
        modules = (shim.parent / "node_modules").resolve(strict=True)
        if not entry.is_relative_to(modules) or not entry.is_file():
            return None
        return entry
    except (OSError, ValueError, UnicodeError):
        return None
