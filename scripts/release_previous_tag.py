#!/usr/bin/env python3
"""Pick the tag a release's generated notes are compared against.

GitHub's generated release notes list the pull requests between a release tag
and a "previous" tag. Left to choose, GitHub takes the newest release tag that
is an ANCESTOR of the new one. Releases here are tagged on per-version release
branches cut from main, so no recent release tag is an ancestor of the next
one, and the choice falls back to a tag from the first weeks of the project:
every page then lists thousands of old pull requests. So the workflow names
the base itself, by lineage rather than by ancestry:

* a stable tag ``vX.Y.Z`` compares against the highest stable tag below
  ``X.Y.Z``;
* a prerelease tag ``vX.Y.Z-<label>.N`` compares against the prerelease of the
  same ``X.Y.Z`` line with the highest number below ``N`` (the label may
  change along a line: ``rc.1`` starts it and ``insider.2`` follows);
* the first prerelease of a line compares against the highest stable tag below
  its base, which is what that line adds over the last stable release.

Tags are read with ``scripts/check_feed_advance.py::parse_version``, the parser
the publish jobs already use, so both agree on which spellings are release tags
and on a prerelease's number (the text after its last dot). Other names are
ignored. When there is no candidate the output is empty, and the release action
falls back to GitHub's own choice.

Every tag that parses is a candidate, including a prerelease tag whose release
run failed and published no page; the next prerelease's notes then start from
that tag. The notes stay editable on the release page; issue #25077 tracks
reading published releases instead of tag refs.

Usage: ``release_previous_tag.py <tag>`` with the repository's tag names, one
per line (``refs/tags/`` prefixes allowed), on stdin. Prints the chosen tag.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable, Iterable, NamedTuple


def _load_parse_version() -> Callable[[str], tuple[int, int, int, int, int] | None]:
    """Load ``parse_version`` from the sibling script by path (scripts/ is no package)."""
    path = Path(__file__).resolve().with_name("check_feed_advance.py")
    spec = importlib.util.spec_from_file_location("_release_check_feed_advance", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse_version  # type: ignore[no-any-return]


parse_version = _load_parse_version()


class ReleaseTag(NamedTuple):
    name: str
    base: tuple[int, int, int]
    # None for a stable tag; the prerelease's trailing number otherwise.
    number: int | None


def parse_tag(name: str) -> ReleaseTag | None:
    """Return the tag's base and prerelease number, or None for other shapes."""
    name = name.strip().removeprefix("refs/tags/")
    if not name.startswith("v"):
        return None
    key = parse_version(name)
    if key is None:
        return None
    major, minor, patch, is_release, number = key
    return ReleaseTag(
        name=name,
        base=(major, minor, patch),
        number=None if is_release else number,
    )


def previous_tag(current: str, tags: Iterable[str]) -> str:
    """Return the tag *current*'s notes compare against, or "" when none fits."""
    this = parse_tag(current)
    if this is None:
        return ""
    known = [tag for tag in (parse_tag(name) for name in tags) if tag is not None]
    if this.number is not None:
        same_line = [
            tag
            for tag in known
            if tag.base == this.base and tag.number is not None and tag.number < this.number
        ]
        if same_line:
            return max(same_line, key=lambda tag: tag.number or 0).name
    stable = [tag for tag in known if tag.number is None and tag.base < this.base]
    return max(stable, key=lambda tag: tag.base).name if stable else ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_previous_tag.py <tag>  (tag names on stdin)", file=sys.stderr)
        return 2
    print(previous_tag(argv[1], sys.stdin.read().split()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
