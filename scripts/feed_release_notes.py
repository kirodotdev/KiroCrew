#!/usr/bin/env python3
"""Print a published version's ``CHANGELOG.md`` section as the feed's ``releaseNotes``.

The desktop app shows a pending update's notes from the channel file's
``releaseNotes`` (``notesFrom`` in ``website/electron/runtime/update/feed-lane.js``),
and the publish lanes write that file by hand; this script supplies the field. The
section is picked the way Settings > Releases picks the installed build's row
(``changelog.build_release_list``): ``0.4.0-insider.14`` reads ``## [0.4.0]``, and a
version with no section (a nightly) gets empty notes.

Usage: feed_release_notes.py <version>   -> prints one JSON string

A JSON string is also a YAML double-quoted scalar, printed on one line in plain
ASCII: no changelog text can break the channel file, and no line of it can pass
for the top-level ``version:`` that ``check_feed_advance.py`` scans for.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Loaded by path: the publish jobs check the tree out without installing
# kiro_crew, and changelog.py imports only the standard library.
_spec = importlib.util.spec_from_file_location(
    "_kc_changelog", ROOT / "src" / "kiro_crew" / "changelog.py"
)
assert _spec and _spec.loader
changelog = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(changelog)


def release_notes(markdown: str, version: str) -> str:
    """The body of *version*'s section in *markdown*, or ``""`` when it has none."""
    releases = changelog.build_release_list(markdown, version)
    return next((release.body for release in releases if release.is_current), "")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: feed_release_notes.py <version>", file=sys.stderr)
        return 2
    markdown = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    print(json.dumps(release_notes(markdown, argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
